"""Strict tier: declared dependencies and their licences from ClearlyDefined.

Only public package coordinates (for example npm/npmjs/-/is-odd/3.0.1) leave the machine. They
come from the project's own manifests (package.json, package-lock.json, requirements*.txt,
pyproject.toml); manifests inside vendored directories belong to the dependencies themselves and
are never read. Only direct, declared dependencies with an exact version are looked up.

Privacy rules (a name sent to the public service cannot be taken back):
- a dependency the manifests show comes from a private registry or index is excluded
  (`private_registry`); when the project configures an extra index and the source of a package
  cannot be told, it is excluded too (`private_registry_possible`);
- registry settings are read from .npmrc, .yarnrc.yml and pip.conf by two or three narrow line
  patterns only (registry URLs); tokens and auth lines are never read, and URLs (which may carry
  credentials) are compared by host and never stored;
- scoped npm packages are sent in their own requests so a whole scope can be struck.

`plan` writes the query file; `run` sends only what an approved plan lists. Names that do not
look like public package names are excluded with a reason and never sent. The plan's `excluded`
section holds counts per manifest and reason, never dependency names.
"""
from __future__ import annotations

import argparse
import json
import math
import posixpath
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approval  # noqa: E402
import net  # noqa: E402
from approval import ApprovalError, Guard, build_plan, load_plan, render_preview, write_plan  # noqa: E402
from inventory import VENDOR_DIRS, Inventory, InventoryError, inventory  # noqa: E402
from model import new_results, write_results  # noqa: E402
from replay import RecordingOpener  # noqa: E402

try:
    import tomllib
except ImportError:                                  # Python < 3.11
    tomllib = None

CD_URL = "https://api.clearlydefined.io/definitions"
CD_BATCH = 50
CD_QUOTA = "POST /definitions: 250 requests per 60 s (RateLimit-Policy 250;w=60)"
MAX_DEPENDENCIES = 5000
MAX_SCOPE_REQUESTS = 25
MAX_INCLUDE_DEPTH = 5
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_CONFIG_BYTES = 256 * 1024
MAX_LIST = 50
MAX_TEXT = 300
MAX_LINE = 2000

PUBLIC_NPM_HOSTS = frozenset({"registry.npmjs.org", "registry.yarnpkg.com"})
GIT_HOSTS = frozenset({"codeload.github.com", "github.com", "gitlab.com", "bitbucket.org",
                       "raw.githubusercontent.com", "api.github.com"})
PUBLIC_PY_HOSTS = frozenset({"pypi.org", "pypi.python.org", "files.pythonhosted.org"})

_NPM_NAME = re.compile(r"(?:@[A-Za-z0-9][A-Za-z0-9._~-]{0,213}/)?[A-Za-z0-9][A-Za-z0-9._~-]{0,213}")
_PYPI_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_!~-]{0,63}")
_SEMVER = re.compile(r"v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)")
_LOCAL_PREFIXES = ("file:", "link:", "git+", "git:", "github:", "gitlab:", "bitbucket:", "http:", "https:",
                   "ssh:", "workspace:", "portal:", "patch:", "git@")
_WIN_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
_REQ_NAME = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)", re.S)
_REQ_PIN = re.compile(r"===?\s*([^\s,;*]+)")
_REQ_REST_OK = re.compile(r"(?:[<>=!~]|$)")
_REQ_OPT_CUT = re.compile(r"\s+--?[A-Za-z]")
_INCLUDE = re.compile(r"(-r|--requirement|-c|--constraint)(?:=|\s+|(?=[^-\s]))(.+)$")
_INCLUDE_SCAN = re.compile(r"(?m)^\s*(?:-r|--requirement)(?:=|[ \t]+|(?=[^-\s]))[ \t]*(\S+)")
_INDEX_URL = re.compile(r"(?:-i|--index-url)(?:=|\s+)(\S+)")
_EXTRA_INDEX = re.compile(r"--extra-index-url(?:=|\s+)(\S+)")
_FIND_LINKS = re.compile(r"(?:-f|--find-links)(?:=|\s+)(\S+)")
_EDITABLE = re.compile(r"(?:-e|--editable)(?:\s|=|$)")
_MANIFEST_NAMES = frozenset({
    "package.json", "package-lock.json", "pyproject.toml", "go.mod", "go.sum", "cargo.toml", "cargo.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "gemfile", "gemfile.lock", "composer.json", "yarn.lock",
    "pnpm-lock.yaml", "pipfile", "pipfile.lock", "poetry.lock", "pubspec.yaml", "uv.lock", "setup.py",
    "setup.cfg", "environment.yml", "environment.yaml"})
_EXTRA_UNSUPPORTED = frozenset({"setup.py", "setup.cfg", "environment.yml", "environment.yaml", "uv.lock"})
_NOT_PIP_TOOLS = frozenset({"h", "c", "cc", "cpp", "am", "ac", "py", "sh", "pc", "cmake", "xml", "txt", "cfg",
                            "json", "js", "html", "in"})
_BLOCKED_SKIPS = frozenset({"sensitive", "vendored", "gitignored", "unreadable"})


@dataclass
class Dependency:
    ecosystem: str          # npm | pypi (unknown for an unsupported manifest)
    name: str
    version: str | None
    manifest: str
    excluded_reason: str | None
    # excluded_reason: unpinned|local_or_git|private_registry|private_registry_possible|local_version|alias|
    #   unsupported_manifest|no_tomllib|bad_name|bad_entry|too_many|ignored_manifest
    count: int = 1


# ---- helpers ------------------------------------------------------------------------

def _pathlike(text: str) -> bool:
    low = text.lower()
    return (low.startswith(_LOCAL_PREFIXES) or text[:1] in (".", "/", "\\") or text.startswith(("~/", "~\\"))
            or "://" in text or bool(_WIN_DRIVE.match(text)))


def _safe_name(ecosystem: str, name) -> bool:
    if not isinstance(name, str):
        return False
    pat = _NPM_NAME if ecosystem == "npm" else _PYPI_NAME
    return len(name) <= 214 and pat.fullmatch(name) is not None


def _safe_version(version) -> bool:
    return isinstance(version, str) and _VERSION.fullmatch(version) is not None


def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def coordinate(dep: Dependency) -> str | None:
    if dep.excluded_reason or dep.version is None or dep.ecosystem not in ("npm", "pypi"):
        return None
    if not _safe_name(dep.ecosystem, dep.name) or not _safe_version(dep.version):
        return None
    if dep.ecosystem == "npm":
        if dep.name.startswith("@"):
            scope, _, name = dep.name.partition("/")
            return f"npm/npmjs/{scope}/{name}/{dep.version}"
        return f"npm/npmjs/-/{dep.name}/{dep.version}"
    if "+" in dep.version:
        return None                                   # PEP 440 local label: often an internal build
    return f"pypi/pypi/-/{_norm_py(dep.name)}/{dep.version}"


def _short(text) -> str:
    return str(text)[:200]


def _host(url) -> str | None:
    """Host of a URL, or None. Credentials in the URL are never kept."""
    try:
        return urlsplit(str(url).strip().strip("\"'")).hostname or None
    except ValueError:
        return None


class _Collector:
    def __init__(self):
        self.items: list[Dependency] = []
        self.forced: list[Dependency] = []
        self.dropped: Counter = Counter()

    @property
    def full(self) -> bool:
        return len(self.items) >= MAX_DEPENDENCIES

    def add(self, ecosystem, name, version, manifest, reason=None, force=False, count=1) -> None:
        if force:
            self.forced.append(Dependency(ecosystem, _short(name), None, manifest, reason, count))
            return
        if self.full:
            self.dropped[manifest] += count
            return
        if reason is None:
            if not _safe_name(ecosystem, name) or (version is not None and not _safe_version(version)):
                reason = "local_or_git" if isinstance(name, str) and _pathlike(name) else "bad_name"
            elif ecosystem == "pypi" and version is not None and "+" in version:
                reason = "local_version"
        self.items.append(Dependency(ecosystem, _short(name), version if reason is None else None,
                                     manifest, reason, count))

    def finish(self) -> list[Dependency]:
        more = [Dependency("unknown", "(more dependencies)", None, m, "too_many", n)
                for m, n in sorted(self.dropped.items())]
        return self.items + more + self.forced


class _Ctx:
    """Reads files of one inventory with caches; the only place that touches the disk."""

    def __init__(self, inv: Inventory):
        self.inv = inv
        self.col = _Collector()
        self._bytes: dict = {}
        self._npm_conf: dict = {}
        self._pip_conf: dict = {}
        self._blocked = {s.rel for s in inv.skipped if s.reason in _BLOCKED_SKIPS}

    def load(self, rel: str, limit: int = MAX_MANIFEST_BYTES) -> bytes | None:
        path = self.inv.root / rel
        try:
            if not path.resolve().is_relative_to(self.inv.root) or path.stat().st_size > limit:
                return None
            return path.read_bytes()
        except (OSError, ValueError):
            return None

    def text(self, rel: str) -> str | None:
        if rel not in self._bytes:
            raw = self.load(rel)
            self._bytes[rel] = None if raw is None else _decode(raw)
        return self._bytes[rel]

    def json(self, rel: str):
        text = self.text(rel)
        if text is None:
            return None
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            return None

    def followable(self, rel: str) -> bool:
        if rel in self._blocked or any(p in VENDOR_DIRS for p in rel.split("/")[:-1]):
            return False
        try:
            return (self.inv.root / rel).is_file()
        except OSError:
            return False

    # -- registry configuration (narrow patterns only) --

    def _dirs(self, dir_rel: str):
        d = dir_rel
        while True:
            yield d
            if not d:
                return
            d = posixpath.dirname(d)

    def npm_conf(self, dir_rel: str):
        """(default_registry_is_private, {private scopes in lower case})"""
        if dir_rel in self._npm_conf:
            return self._npm_conf[dir_rel]
        default, scopes = False, set()
        for d in self._dirs(dir_rel):
            for fname in (".npmrc", ".yarnrc.yml"):
                raw = self.load(posixpath.join(d, fname) if d else fname, MAX_CONFIG_BYTES)
                if raw is None:
                    continue
                lines = _decode(raw).splitlines()[:2000]
                if fname == ".npmrc":
                    de, sc = _parse_npmrc(lines)
                else:
                    de, sc = _parse_yarnrc(lines)
                default, scopes = default or de, scopes | sc
        self._npm_conf[dir_rel] = (default, scopes)
        return default, scopes

    def pip_conf(self, dir_rel: str) -> dict:
        if dir_rel in self._pip_conf:
            return dict(self._pip_conf[dir_rel])
        state = {"index_private": False, "extra": False}
        for d in self._dirs(dir_rel):
            for fname in ("pip.conf", "pip.ini"):
                raw = self.load(posixpath.join(d, fname) if d else fname, MAX_CONFIG_BYTES)
                if raw is None:
                    continue
                for line in _decode(raw).splitlines()[:2000]:
                    m = re.match(r"\s*index[-_]url\s*[=:]\s*(\S+)", line)
                    if m and _host(m.group(1)) not in PUBLIC_PY_HOSTS:
                        state["index_private"] = True
                    m = re.match(r"\s*(?:extra[-_]index[-_]url|find[-_]links)\s*[=:]\s*(\S+)", line)
                    if m and _host(m.group(1)) not in PUBLIC_PY_HOSTS:
                        state["extra"] = True
        self._pip_conf[dir_rel] = state
        return dict(state)


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeError:
            pass
    return raw.decode("utf-8-sig", errors="replace")


def _parse_npmrc(lines):
    default, scopes = False, set()
    for line in lines:
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        m = re.match(r"(@[A-Za-z0-9][A-Za-z0-9._~-]*):registry\s*=\s*(.*)$", s)
        if m:
            if _host(m.group(2)) not in PUBLIC_NPM_HOSTS:
                scopes.add(m.group(1).lower())
            continue
        m = re.match(r"registry\s*=\s*(.*)$", s)
        if m and _host(m.group(1)) not in PUBLIC_NPM_HOSTS:
            default = True
    return default, scopes


def _parse_yarnrc(lines):
    default, scopes, in_scopes, cur = False, set(), False, None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[0].isspace()
        if not indented:
            in_scopes, cur = bool(re.match(r"npmScopes\s*:", line)), None
            m = re.match(r"npmRegistryServer\s*:\s*(.*)$", line)
            if m and _host(m.group(1)) not in PUBLIC_NPM_HOSTS:
                default = True
            continue
        if not in_scopes:
            continue
        m = re.match(r"\s+[\"']?([A-Za-z0-9][A-Za-z0-9._~-]*)[\"']?\s*:\s*$", line)
        if m:
            cur = m.group(1).lower()
            continue
        m = re.match(r"\s+npmRegistryServer\s*:\s*(.*)$", line)
        if m and cur and _host(m.group(1)) not in PUBLIC_NPM_HOSTS:
            scopes.add("@" + cur)
    return default, scopes


# ---- npm ----------------------------------------------------------------------------

def _npm_spec(spec):
    """-> (version | None, reason | None)"""
    if not isinstance(spec, str):
        return None, "unpinned"
    s = spec.strip()
    if s.lower().startswith("npm:"):
        return None, "alias"                          # registry alias: the real name is not the key
    if _pathlike(s) or "/" in s:
        return None, "local_or_git"
    m = _SEMVER.fullmatch(s[1:] if s.startswith("=") else s)
    return (m.group(1), None) if m else (None, "unpinned")


def _resolved_class(resolved) -> str | None:
    """public | git | private | None (no information)."""
    if not isinstance(resolved, str) or not resolved:
        return None
    low = resolved.lower()
    if low.startswith(("file:", "link:", "git+", "git:", "git@", "ssh:", "github:")):
        return "git"
    host = _host(resolved)
    if host is None:
        return "private"
    if host in PUBLIC_NPM_HOSTS:
        return "public"
    return "git" if host in GIT_HOSTS else "private"


def _lock_entries(lock) -> dict:
    """name -> {version, name, local, private} for the top-level packages of a lockfile."""
    out: dict = {}
    if not isinstance(lock, dict):
        return out

    def make(entry):
        cls = _resolved_class(entry.get("resolved"))
        real = entry.get("name")
        return {"version": entry.get("version"), "name": real if isinstance(real, str) else None,
                "local": bool(entry.get("link")) or cls == "git", "private": cls == "private"}

    pkgs = lock.get("packages")
    if isinstance(pkgs, dict):
        for key, entry in pkgs.items():
            if not (isinstance(key, str) and key.startswith("node_modules/") and isinstance(entry, dict)):
                continue
            name = key[len("node_modules/"):]
            if "node_modules/" in name:
                continue                              # nested: a transitive copy
            out[name] = make(entry)
        return out
    old = lock.get("dependencies")
    if isinstance(old, dict):
        for name, entry in old.items():
            if isinstance(entry, dict) and isinstance(name, str):
                out[name] = make(entry)
    return out


def _lock_version(entry):
    v = entry.get("version") if entry else None
    m = _SEMVER.fullmatch(v) if isinstance(v, str) else None
    return m.group(1) if m else None


def _npm_private(conf, name) -> bool:
    default, scopes = conf
    return default or (isinstance(name, str) and name.startswith("@") and name.split("/")[0].lower() in scopes)


def _read_npm(ctx, rel, lock_rel):
    col = ctx.col
    data = ctx.json(rel)
    if not isinstance(data, dict):
        col.add("unknown", f"(unreadable {rel})", None, rel, "unsupported_manifest", force=True)
        return
    lock = _lock_entries(ctx.json(lock_rel)) if lock_rel else {}
    conf = ctx.npm_conf(posixpath.dirname(rel))
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        if section not in data:
            continue
        block = data[section]
        if not isinstance(block, dict):
            col.add("unknown", f"({section} is not an object)", None, rel, "bad_entry")
            continue
        if col.full:
            col.dropped[rel] += len(block)
            continue
        for name, spec in block.items():
            version, reason = _npm_spec(spec)
            entry = lock.get(name) if isinstance(name, str) else None
            real = name
            if reason != "local_or_git" and entry is not None:
                if entry["local"]:
                    version, reason = None, "local_or_git"
                elif entry["private"]:
                    version, reason = None, "private_registry"
                else:
                    if reason in ("unpinned", "alias"):
                        lv = _lock_version(entry)
                        version, reason = (lv, None) if lv else (None, reason)
                    if entry["name"] and entry["name"] != name:
                        real = entry["name"]
            if reason is None and _npm_private(conf, real):
                version, reason = None, "private_registry"
            col.add("npm", real, version, rel, reason)


def _read_npm_lock_only(ctx, rel):
    col = ctx.col
    lock = ctx.json(rel)
    if not isinstance(lock, dict):
        col.add("unknown", f"(unreadable {rel})", None, rel, "unsupported_manifest", force=True)
        return
    conf = ctx.npm_conf(posixpath.dirname(rel))
    entries = _lock_entries(lock)
    if col.full:
        col.dropped[rel] += len(entries)
        return
    for key, entry in entries.items():
        name = entry["name"] or key
        version = _lock_version(entry)
        if entry["local"]:
            reason = "local_or_git"
        elif entry["private"] or _npm_private(conf, name):
            reason = "private_registry"
        else:
            reason = None if version else "unpinned"
        col.add("npm", name, version, rel, reason)


# ---- python -------------------------------------------------------------------------

def _requirement(line: str):
    """One PEP 508 style requirement -> (name, version, reason)."""
    if len(line) > MAX_LINE:
        return line[:200], None, "bad_name"
    line = line.split(";", 1)[0]
    line = _REQ_OPT_CUT.split(line, maxsplit=1)[0].strip()
    if not line:
        return None
    if _pathlike(line) or re.match(r"[^\s=<>!~]+\s*@\s*\S", line):
        return line, None, "local_or_git"
    m = _REQ_NAME.fullmatch(line)
    if not m or not _REQ_REST_OK.match(m.group(3)):
        return line, None, "bad_name"
    name, rest = m.group(1), m.group(3).strip()
    pin = _REQ_PIN.fullmatch(rest) if rest.startswith("==") else None
    if pin and _safe_version(pin.group(1)):
        return name, pin.group(1), None
    return name, None, "unpinned"


def _req_lines(text: str):
    for raw in text.splitlines():
        line = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        while line.endswith("\\"):
            line = line[:-1].rstrip()
        if line:
            yield line


def _resolve_include(from_rel: str, target: str) -> str | None:
    t = target.strip().strip("\"'")
    if (not t or len(t) > 300 or "\x00" in t or "://" in t or t[0] in "/\\~$" or _WIN_DRIVE.match(t)):
        return None
    t = t.replace("\\", "/")
    if ".." in t.split("/"):
        return None
    r = posixpath.normpath(posixpath.join(posixpath.dirname(from_rel), t))
    return None if r in ("", ".") or r.startswith(("/", "..")) else r


def _label(rel: str | None) -> str:
    if rel and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,200}", rel) and ".." not in rel:
        return rel
    return "(include outside the project)"


def _read_requirements_tree(ctx, root_rel: str) -> set:
    """Read a requirements file and the files it includes; index options apply to the whole tree."""
    col = ctx.col
    state = ctx.pip_conf(posixpath.dirname(root_rel))
    deps_found: list = []         # (name, version, reason, manifest)
    unsupported: list = []        # labels
    seen: set = set()

    def pending() -> int:
        return len(col.items) + len(deps_found)

    def visit(rel: str, depth: int):
        if rel in seen:
            return
        seen.add(rel)
        text = ctx.text(rel)
        if text is None:
            unsupported.append(_label(rel))
            return
        for line in _req_lines(text):
            if line[0] == "-":
                m = _INCLUDE.match(line)
                if m:
                    resolved = _resolve_include(rel, m.group(2))
                    is_req = m.group(1) in ("-r", "--requirement")
                    if (is_req and resolved is not None and depth < MAX_INCLUDE_DEPTH
                            and ctx.followable(resolved)):
                        visit(resolved, depth + 1)
                    else:
                        unsupported.append(_label(resolved))
                    continue
                m = _INDEX_URL.match(line)
                if m:
                    state["index_private"] = _host(m.group(1)) not in PUBLIC_PY_HOSTS
                    continue
                m = _EXTRA_INDEX.match(line)
                if m:
                    if _host(m.group(1)) not in PUBLIC_PY_HOSTS:
                        state["extra"] = True
                    continue
                if _FIND_LINKS.match(line):
                    state["extra"] = True
                    continue
                if _EDITABLE.match(line):
                    if pending() >= MAX_DEPENDENCIES:
                        col.dropped[rel] += 1
                    else:
                        deps_found.append((line, None, "local_or_git", rel))
                continue
            if pending() >= MAX_DEPENDENCIES:
                col.dropped[rel] += 1
                continue
            parsed = _requirement(line)
            if parsed:
                deps_found.append((*parsed, rel))

    visit(root_rel, 0)
    verdict = ("private_registry" if state["index_private"]
               else "private_registry_possible" if state["extra"] else None)
    for name, version, reason, rel in deps_found:
        if reason is None and verdict:
            version, reason = None, verdict
        col.add("pypi", name, version, rel, reason)
    for label in unsupported:
        col.add("unknown", label, None, label, "unsupported_manifest", force=True)
    return seen


def _pyproject_sources(data):
    """(project has a non-PyPI index, {uv source name: public?})"""
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    uv = tool.get("uv") if isinstance(tool.get("uv"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    pdm = tool.get("pdm") if isinstance(tool.get("pdm"), dict) else {}
    entries: list = []
    for block in (poetry.get("source"), pdm.get("source"), uv.get("index")):
        if isinstance(block, dict):
            entries.append(block)
        elif isinstance(block, list):
            entries.extend(b for b in block if isinstance(b, dict))
    urls = [e.get("url") for e in entries if isinstance(e.get("url"), str)]
    for key in ("index-url", "extra-index-url"):
        v = uv.get(key)
        urls.extend([v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] if isinstance(v, list) else [])
    private = any(_host(u) not in PUBLIC_PY_HOSTS for u in urls)
    names = {e["name"]: _host(e.get("url")) in PUBLIC_PY_HOSTS for e in entries
             if isinstance(e.get("name"), str) and isinstance(e.get("url"), str)}
    return private, names


def _poetry_dep(col, name, spec, rel, verdict, uv_sources, index_public):
    reason = None
    version = None
    if isinstance(spec, dict):
        if "source" in spec:
            reason = "private_registry"
        elif any(k in spec for k in ("git", "path", "url")):
            reason = "local_or_git"
        spec = spec.get("version")
    if reason is None:
        if isinstance(spec, str) and re.fullmatch(r"\d+(?:\.\d+)*(?:[A-Za-z0-9.+-]*)", spec.strip()):
            version = spec.strip()
        else:
            reason = "unpinned"
    _add_py(col, name, version, rel, reason, verdict, uv_sources, index_public)


def _add_py(col, name, version, rel, reason, verdict, uv_sources, index_public):
    if isinstance(name, str) and isinstance(uv_sources, dict):
        src = uv_sources.get(name) or uv_sources.get(_norm_py(name))
        if isinstance(src, dict) and reason in (None, "unpinned"):
            if "index" in src:
                if not index_public.get(src["index"], False):
                    reason = "private_registry"
            elif any(k in src for k in ("git", "path", "url", "workspace")):
                reason = "local_or_git"
    if reason is None and verdict:
        version, reason = None, verdict
    col.add("pypi", name, version, rel, reason)


def _read_pyproject(ctx, rel):
    col = ctx.col
    if tomllib is None:
        col.add("pypi", "(pyproject.toml)", None, rel, "no_tomllib", force=True)
        return
    text = ctx.text(rel)
    try:
        data = tomllib.loads(text) if text is not None else None
    except (tomllib.TOMLDecodeError, RecursionError, ValueError):
        data = None
    if not isinstance(data, dict):
        col.add("unknown", f"(unreadable {rel})", None, rel, "unsupported_manifest", force=True)
        return
    private, index_public = _pyproject_sources(data)
    conf = ctx.pip_conf(posixpath.dirname(rel))
    verdict = ("private_registry" if conf["index_private"] else
               "private_registry_possible" if (private or conf["extra"]) else None)
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    uv = tool.get("uv") if isinstance(tool.get("uv"), dict) else {}
    uv_sources = uv.get("sources") if isinstance(uv.get("sources"), dict) else {}
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    lists = []
    if isinstance(project.get("dependencies"), list):
        lists.append(project["dependencies"])
    opt = project.get("optional-dependencies")
    if isinstance(opt, dict):
        lists.extend(v for v in opt.values() if isinstance(v, list))
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        lists.extend(v for v in groups.values() if isinstance(v, list))
    for items in lists:
        if col.full:
            col.dropped[rel] += len(items)
            continue
        for item in items:
            if not isinstance(item, str):
                col.add("unknown", "(non-string dependency entry)", None, rel, "bad_entry")
                continue
            parsed = _requirement(item.strip())
            if parsed:
                _add_py(col, *parsed[:2], rel, parsed[2], verdict, uv_sources, index_public)
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    blocks = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
    pgroups = poetry.get("group")
    if isinstance(pgroups, dict):
        blocks.extend(g.get("dependencies") for g in pgroups.values() if isinstance(g, dict))
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if col.full:
            col.dropped[rel] += len(block)
            continue
        for name, spec in block.items():
            if name != "python":
                _poetry_dep(col, name, spec, rel, verdict, uv_sources, index_public)


# ---- public API ---------------------------------------------------------------------

def _is_pip_tools_in(low: str) -> bool:
    if not low.endswith(".in"):
        return False
    stem = low[:-3]
    return "." not in stem or stem.rsplit(".", 1)[1] not in _NOT_PIP_TOOLS


def _manifest_like(low: str) -> bool:
    return (low in _MANIFEST_NAMES or bool(re.match(r"requirements[^/]*\.txt$", low)) or _is_pip_tools_in(low))


def read_dependencies(inv: Inventory) -> list[Dependency]:
    """Declared dependencies from the project's own manifests (never from vendored directories)."""
    ctx = _Ctx(inv)
    col = ctx.col
    rels = {m.rel for m in inv.manifests}
    requirements = [m.rel for m in inv.manifests if m.kind == "requirements"]
    # requirements files included by another one are read through it, so its index options apply
    included = set()
    for rel in requirements:
        for target in _INCLUDE_SCAN.findall(ctx.text(rel) or ""):
            r = _resolve_include(rel, target)
            if r:
                included.add(r)
    consumed: set = set()
    for m in inv.manifests:
        prefix = posixpath.dirname(m.rel)
        prefix = prefix + "/" if prefix else ""
        if m.kind == "package.json":
            lock = prefix + "package-lock.json"
            _read_npm(ctx, m.rel, lock if lock in rels else None)
        elif m.kind == "package-lock.json":
            if prefix + "package.json" not in rels:
                _read_npm_lock_only(ctx, m.rel)
        elif m.kind == "pyproject":
            _read_pyproject(ctx, m.rel)
        elif m.kind == "requirements":
            if m.rel not in included:
                consumed |= _read_requirements_tree(ctx, m.rel)
        elif m.kind == "unsupported":
            col.add("unknown", m.rel, None, m.rel, "unsupported_manifest", force=True)
    for rel in requirements:                              # include cycles: nobody is the root
        if rel not in consumed:
            consumed |= _read_requirements_tree(ctx, rel)
    # manifests the scanner cannot read, and ignored ones
    ignored = 0
    for rel in [s.rel for s in inv.sources] + [s.rel for s in inv.skipped if s.reason in
                                                  ("not_source", "binary", "too_large", "minified", "generated")]:
        low = posixpath.basename(rel).lower()
        if rel not in rels and (low in _EXTRA_UNSUPPORTED or _is_pip_tools_in(low)):
            col.add("unknown", rel, None, rel, "unsupported_manifest", force=True)
    for s in inv.skipped:
        if s.reason == "gitignored" and _manifest_like(posixpath.basename(s.rel).lower()):
            ignored += 1
    if ignored:
        col.add("unknown", "(ignored manifests)", None, "(gitignored files)", "ignored_manifest", force=True,
                count=ignored)
    return col.finish()


def _scope_of(coord: str) -> str:
    parts = coord.split("/")
    return parts[2] if parts[0] == "npm" and len(parts) > 2 and parts[2].startswith("@") else ""


def make_plan(root: Path) -> dict:
    inv = inventory(Path(root), require_sources=False)
    found = read_dependencies(inv)
    coords: dict[str, list[str]] = {}
    excluded: Counter = Counter()
    for dep in found:
        c = coordinate(dep)
        if c is None:
            excluded[(dep.manifest, dep.excluded_reason or "bad_name")] += dep.count
        else:
            where = coords.setdefault(c, [])
            if dep.manifest not in where:
                where.append(dep.manifest)
    if not found:
        excluded[("(project)", "no_manifests")] += 1
    try:
        return _assemble(inv, coords, excluded)
    except ApprovalError as exc:
        if exc.code != "plan_invalid":
            raise
    # a dependency name that collides with local data (for example one named like a manifest file)
    # must not make the whole plan unbuildable: exclude just that dependency
    probe = {"manifests": sorted({m for ms in coords.values() for m in ms}), "coordinate_manifests": {}}
    keep, bad = {}, []
    for c, ms in coords.items():
        if _collides(c, probe):
            bad.append(c)
        else:
            keep[c] = ms
    if not bad:
        raise
    for c in bad:
        excluded[(coords[c][0], "bad_name")] += 1
    return _assemble(inv, keep, excluded)


def _collides(coord: str, local_only: dict) -> bool:
    body = json.dumps([coord])
    mini = {"local_only": local_only, "requests": [{"url": CD_URL, "body": body}], "followups": []}
    try:
        approval._check_containment(mini)
    except ApprovalError:
        return True
    return bool(approval._has_absolute_path(body))


def _batches(items: list):
    for i in range(0, len(items), CD_BATCH):
        yield items[i:i + CD_BATCH]


def _assemble(inv, coords: dict, excluded: Counter) -> dict:
    manifests: list[str] = []
    for ms in coords.values():
        for m in ms:
            if m not in manifests:
                manifests.append(m)
    unscoped = sorted(c for c in coords if not _scope_of(c))
    by_scope: dict[str, list[str]] = {}
    for c in sorted(coords):
        if _scope_of(c):
            by_scope.setdefault(_scope_of(c), []).append(c)
    scope_names = sorted(by_scope)
    own, rest = scope_names[:MAX_SCOPE_REQUESTS], scope_names[MAX_SCOPE_REQUESTS:]
    groups = [(c, f"{len(c)} unscoped dependencies") for c in _batches(unscoped)]
    for s in own:
        groups += [(b, f"{len(b)} packages of scope {s} (strike this request if the scope is private)")
                   for b in _batches(by_scope[s])]
    merged = sorted(c for s in rest for c in by_scope[s])
    groups += [(b, f"{len(b)} packages of other scopes (strike this request if one of the scopes is private)")
               for b in _batches(merged)]
    requests, mapping = [], {}
    for n, (batch, what) in enumerate(groups, 1):
        rid = f"cd-{n}"
        requests.append({
            "id": rid, "method": "POST", "url": CD_URL,
            "body": json.dumps(batch, separators=(",", ":")),
            "headers": {"Content-Type": "application/json"},
            "purpose": f"licence lookup for {what} (name and version only)",
            "source": None,
        })
        mapping[rid] = [[manifests.index(m) for m in coords[c]] for c in batch]
    n = len(requests)
    disclosure = []
    if scope_names:
        shown = ", ".join(scope_names[:20]) + (f" and {len(scope_names) - 20} more" if len(scope_names) > 20 else "")
        disclosure.append("Scoped packages are sent by name. If a scope is your organisation's private scope, "
                          f"strike that request. Scopes in this plan: {shown}.")
    disclosure += [
        "Sent: the package name and exact version of declared dependencies, as public package coordinates "
        "(for example npm/npmjs/-/name/1.2.3), to api.clearlydefined.io, a public service. Anything in the "
        "request bodies is visible to that service: strike any request that holds a name that looks private "
        "or internal.",
        "Never sent: dependencies with ranges or local/git specs, dependencies the manifests show come from a "
        "private registry or index (private_registry), and every dependency of a project that configures an "
        "extra index (private_registry_possible).",
        "Not sent: file paths, file contents, the project name.",
        "Only direct dependencies declared in the project's own manifests are looked up; transitive "
        "dependencies are not.",
    ]
    return build_plan(
        backend="clearlydefined", tier="strict", root=str(inv.root), requests=requests, followups=[],
        disclosure=disclosure, cost={"requests": n, "quota": f"{n} of {CD_QUOTA}"},
        excluded=[{"rel": rel, "reason": f"{reason} ({count})" if reason != "no_manifests" else "no_manifests (1)"}
                  for (rel, reason), count in sorted(excluded.items())],
        local_only={"manifests": manifests, "coordinate_manifests": mapping},
    )


def _text(value, limit=MAX_TEXT):
    return value[:limit] if isinstance(value, str) else None


def _str_list(value):
    if not isinstance(value, list):
        return []
    return [v[:MAX_TEXT] for v in value if isinstance(v, str)][:MAX_LIST]


def _dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def parse_definitions(body) -> dict:
    """coordinate -> {declared, discovered, attribution, source_url, release_date, status, confidence,
    known, conflict, truncated}. status: declared | non_spdx | none_found | unknown."""
    out: dict = {}
    if not isinstance(body, dict):
        return out
    for coord, d in body.items():
        if not isinstance(coord, str) or not isinstance(d, dict):
            continue
        eff = _dig(d, "scores", "effective")
        valid = isinstance(eff, (int, float)) and not isinstance(eff, bool) and math.isfinite(eff)
        eff = max(0.0, min(float(eff), 100.0)) if valid else 0.0
        known = valid and eff > 0
        raw = _dig(d, "licensed", "declared")
        declared, truncated, non_spdx = None, False, False
        if isinstance(raw, str):
            s = raw.strip()
            truncated = len(s) > MAX_TEXT
            s = s[:MAX_TEXT]
            if s.upper() == "OTHER":
                declared, non_spdx = s, True
            elif s and s.upper() != "NOASSERTION":
                declared = s
        discovered = _str_list(_dig(d, "licensed", "facets", "core", "discovered", "expressions"))
        if not known:
            status, declared, non_spdx = "unknown", None, False
        elif non_spdx:
            status = "non_spdx"
        else:
            status = "declared" if declared else "none_found"
        out[coord] = {
            "declared": declared,
            "discovered": discovered,
            "attribution": _str_list(_dig(d, "licensed", "facets", "core", "attribution", "parties")),
            "source_url": _text(_dig(d, "described", "sourceLocation", "url")),
            "release_date": _text(_dig(d, "described", "releaseDate")),
            "status": status,
            "confidence": round(eff / 100, 2) if valid else None,
            "known": known,
            "conflict": status == "declared" and bool(discovered) and set(discovered) != {declared},
            "truncated": truncated and status in ("declared", "non_spdx"),
        }
    return out


def _parse_coordinate(coord: str):
    parts = coord.split("/")
    if len(parts) != 5:
        return None
    eco = "npm" if parts[0] == "npm" else "pypi"
    name = parts[3] if parts[2] == "-" else f"{parts[2]}/{parts[3]}"
    return eco, name, parts[4]


def _error(code, detail="", request_id=None):
    err = {"code": code, "detail": detail}
    if request_id:
        err["request_id"] = request_id
    return err


_REASON_NOTES = {
    "private_registry": "dependencies resolved from a private registry or index were not sent",
    "private_registry_possible": "the project configures an extra package index, so the source of each package "
                                 "cannot be told; none of those dependencies was sent",
    "local_version": "PEP 440 local version labels (+...) were not sent",
    "unpinned": "dependencies without an exact version were not looked up",
    "local_or_git": "local, git and URL dependencies were not looked up",
    "alias": "npm aliases without a lockfile entry were not looked up",
    "bad_name": "names that are not valid public package names were not sent",
    "bad_entry": "manifest entries that were not in the expected shape were skipped",
    "too_many": f"only the first {MAX_DEPENDENCIES} dependencies are looked up per run",
    "ignored_manifest": "manifests ignored by .gitignore were not read",
    "unsupported_manifest": "manifests (or includes) this scanner cannot read were not looked up",
    "no_tomllib": "pyproject.toml needs Python 3.11 or newer",
    "no_manifests": "no dependency manifest was found; nothing was looked up",
}


def run(plan_path: Path, approved_sha256: str, opener=None, sleep=time.sleep) -> dict:
    plan = load_plan(plan_path, approved_sha256)          # raises ApprovalError before anything is sent
    guard = Guard(plan)
    res = new_results("clearlydefined", "strict", plan["sha256"], plan["root"])
    local = plan["local_only"]
    manifests = local.get("manifests", []) if isinstance(local.get("manifests"), list) else []
    mapping = local.get("coordinate_manifests", {}) if isinstance(local.get("coordinate_manifests"), dict) else {}
    deps_out: list = []

    def entry(coord, idxs, got):
        eco, name, version = _parse_coordinate(coord) or ("npm", coord, None)
        names = [manifests[i] for i in idxs if isinstance(i, int) and 0 <= i < len(manifests)]
        status = got["status"] if got else "unknown"
        lic = {"spdx": got["declared"] if got and status == "declared" else None,
               "status": status,
               "evidence": "clearlydefined" if got else None,
               "confidence": got["confidence"] if got else None}
        if got and status == "non_spdx":
            lic["raw"] = got["declared"]
        item = {"ecosystem": eco, "name": name, "version": version, "coordinate": coord,
                "manifest": names[0] if names else None, "manifests": names,
                "license": lic, "known": bool(got and got["known"])}
        if got:
            item.update({k: got[k] for k in ("discovered", "attribution", "source_url", "release_date",
                                             "conflict", "truncated")})
        return item

    stopped = False
    for req in plan["requests"]:
        coords = json.loads(req["body"])
        maps = mapping.get(req["id"], [])
        maps = maps if isinstance(maps, list) else []

        def idx_of(i):
            return maps[i] if i < len(maps) and isinstance(maps[i], list) else []

        def not_sent(reason):
            res["not_sent"].append({"id": req["id"], "method": req["method"], "url": req["url"],
                                    "coordinates": len(coords), "reason": reason})
            for i, c in enumerate(coords):
                deps_out.append(entry(c, idx_of(i), None))

        if stopped:
            not_sent("not attempted after the quota was exhausted")
            continue
        try:
            resp = net.send(req["method"], req["url"], guard, body=req["body"].encode("utf-8"),
                            opener=opener, sleep=sleep)
        except Exception as exc:                          # NetError, ApprovalError, blocked sockets, ...
            code = exc.code if isinstance(getattr(exc, "code", None), str) else "network_error"
            res["errors"].append(_error(code, _short(exc), req["id"]))
            not_sent(code)
            continue
        if resp.status == 429:
            res["errors"].append(_error("quota_exhausted", "ClearlyDefined answered HTTP 429", req["id"]))
            not_sent("quota_exhausted")
            stopped = True
            continue
        if resp.status != 200:
            code = "service_unavailable" if resp.status == 503 else f"http_{resp.status}"
            res["errors"].append(_error(code, f"HTTP {resp.status}", req["id"]))
            not_sent(code)
            continue
        try:
            body = json.loads(resp.body.decode("utf-8-sig"))
        except (ValueError, RecursionError):
            body = None
        if not isinstance(body, dict):
            res["errors"].append(_error("bad_response", "the response is not a JSON object", req["id"]))
            not_sent("bad_response")
            continue
        parsed = parse_definitions(body)
        folded = {k.casefold(): v for k, v in parsed.items()}
        missing = 0
        for i, c in enumerate(coords):
            got = parsed.get(c) or folded.get(c.casefold())
            missing += got is None
            deps_out.append(entry(c, idx_of(i), got))
        if missing:
            res["errors"].append(_error("incomplete_response", f"{missing} coordinates had no definition",
                                        req["id"]))
    res["dependencies"] = deps_out
    res["requests_sent"] = list(guard.sent)
    counts: Counter = Counter()
    for e in plan["excluded"]:
        m = re.fullmatch(r"(\w+) \((\d+)\)", str(e.get("reason", "")))
        if m:
            counts[m.group(1)] += int(m.group(2))
    res["excluded_by_reason"] = dict(sorted(counts.items()))
    res["notes"].append("Direct declared dependencies with an exact version only; transitive dependencies "
                        "are not looked up.")
    if not plan["requests"]:
        res["notes"].append("nothing_to_look_up: the plan has no requests, so no licence information was fetched.")
    if counts:
        res["notes"].append("excluded by reason: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for reason in sorted(counts):
        text = _REASON_NOTES.get(reason)
        if text:
            res["notes"].append(f"{reason}: {text}")
    for e in plan["excluded"]:
        res["notes"].append(f"not looked up: {e.get('rel')}: {e.get('reason')}")
    return res


# ---- CLI ----------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": "usage", "detail": message}))
        raise SystemExit(2)


def _write_results(path: Path, res: dict) -> None:
    json.dumps(res, allow_nan=False)                      # results must be strict JSON
    write_results(path, res)


def main(argv: list) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = _Parser(prog="deps.py")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    p = sub.add_parser("plan")
    p.add_argument("root")
    p.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("plan")
    r.add_argument("--sha256", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--record")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            plan = make_plan(Path(args.root))
            write_plan(plan, Path(args.out), exclusive=True)
            print(json.dumps({"plan_path": str(Path(args.out).resolve()), "sha256": plan["sha256"],
                              "preview": render_preview(plan)}, ensure_ascii=False))
            return 0
        opener = RecordingOpener(net.build_default_opener(), Path(args.record)) if args.record else None
        try:
            res = run(Path(args.plan), args.sha256, opener=opener)
        except ApprovalError as exc:
            res = new_results("clearlydefined", "strict", args.sha256, "")
            res["errors"].append(_error(exc.code, str(exc)))
            write_results(Path(args.out), res)
            print(json.dumps({"error": exc.code, "detail": str(exc)}))
            return 2
        _write_results(Path(args.out), res)
        if res["errors"]:
            first = res["errors"][0]
            print(json.dumps({"error": first["code"], "detail": first["detail"], "errors": len(res["errors"]),
                              "not_sent": len(res["not_sent"]), "results_path": str(Path(args.out).resolve())}))
            return 2
        print(json.dumps({"results_path": str(Path(args.out).resolve()), "dependencies": len(res["dependencies"]),
                          "known": sum(1 for d in res["dependencies"] if d["known"]),
                          "requests_sent": len(res["requests_sent"]), "notes": res["notes"]}))
        return 0
    except ApprovalError as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}))
    except InventoryError as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}))
    except (OSError, ValueError):
        print(json.dumps({"error": "output_error", "detail": "file system or encoding error"}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
