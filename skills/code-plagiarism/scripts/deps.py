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
import os
import posixpath
import re
import stat
import sys
import time
from collections import Counter, OrderedDict
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
TEXT_CACHE_MAX = 256
MAX_INCLUDES_FOLLOWED = 500
MAX_UNSUPPORTED_LABELS = 200
MAX_CONTINUED = 64 * 1024
MAX_CONTINUATIONS = 1000
MAX_LOCK_NODES = 400_000
MAX_LIST = 50
MAX_TEXT = 300
MAX_LINE = 2000

CONFIG_NAMES = frozenset({".npmrc", ".yarnrc.yml", ".yarnrc", "pip.conf", "pip.ini", "uv.toml", "poetry.toml",
                          "pyproject.toml", "setup.cfg", "Pipfile"})
PNPM_FILES = ("pnpm-workspace.yaml", ".pnpmfile.cjs")
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
_INDEX_URL = re.compile(r"(?:--index-url|-i)(?:=|\s*)(\S+)")
_EXTRA_INDEX = re.compile(r"--extra-index-url(?:=|\s*)(\S+)")
_FIND_LINKS = re.compile(r"(?:--find-links|-f)(?:=|\s*)(\S+)")
_TRUSTED_HOST = re.compile(r"-{2}trusted-host(?:=|\s*)(\S+)")
_OVERLONG = object()
_EDITABLE = re.compile(r"(?:-e|--editable)(?:\s|=|$)")
_MANIFEST_NAMES = frozenset({
    "package.json", "package-lock.json", "pyproject.toml", "go.mod", "go.sum", "cargo.toml", "cargo.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "gemfile", "gemfile.lock", "composer.json", "yarn.lock",
    "pnpm-lock.yaml", "pipfile", "pipfile.lock", "poetry.lock", "pubspec.yaml", "uv.lock", "setup.py",
    "setup.cfg", "environment.yml", "environment.yaml"})
_EXTRA_UNSUPPORTED = frozenset({"setup.py", "setup.cfg", "environment.yml", "environment.yaml", "uv.lock",
                                "pnpm-workspace.yaml", ".pnpmfile.cjs"})
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


def _public(url, hosts) -> bool:
    """True only for http(s) on exactly one of `hosts` (default port): no look-alikes, no userinfo tricks."""
    try:
        parts = urlsplit(str(url).strip().strip("\"'"))
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and host in hosts and port in (None, 80, 443)


def _inside_root(root, path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _is_label(name) -> bool:
    return isinstance(name, str) and name in ("<url-spec>", "<vcs-spec>", "<path-spec>", "<invalid-name>")


def _spec_label(name) -> str:
    """A label for an entry that is not a package name; never holds the raw text (it may hold secrets)."""
    s = re.sub(r"^(?:-e|--editable)[\s=]*", "", name) if isinstance(name, str) else ""
    low = s.lower()
    if any(m in low for m in ("git+", "hg+", "svn+", "bzr+")) or low.startswith(("git:", "git@", "ssh:", "github:",
                                                                                  "gitlab:", "bitbucket:")):
        return "<vcs-spec>"
    if "://" in s or " @ " in s or low.startswith(("http:", "https:", "file:")):
        return "<url-spec>"
    if _pathlike(s):
        return "<path-spec>"
    return "<invalid-name>"


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
        if not _safe_name(ecosystem, name) and ecosystem != "unknown" and not _is_label(name):
            label = _spec_label(name)
            if reason in (None, "bad_name") and label != "<invalid-name>":
                reason = "local_or_git"
            name = label
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
        self._bytes: OrderedDict = OrderedDict()
        self._probe: dict = {}
        self._dirlist: dict = {}
        self._npm_conf: dict = {}
        self._pip_conf: dict = {}
        self._lock_idx: dict = {}
        self._pj: dict = {}
        self.follows = 0
        self.member_names: set = set()
        self.known = ({s.rel for s in inv.sources} | {s.rel for s in inv.skipped} | {m.rel for m in inv.manifests})
        self._blocked = {s.rel for s in inv.skipped if s.reason in _BLOCKED_SKIPS}

    def load(self, rel: str, limit: int = MAX_MANIFEST_BYTES) -> bytes | None:
        path = self.inv.root / rel
        try:
            # files listed by the inventory sit in real directories; only the file itself can be a link
            if rel not in self.known or stat.S_ISLNK(os.lstat(path).st_mode):
                if not _inside_root(self.inv.root, path):
                    return None
            with open(path, "rb") as fh:
                if os.fstat(fh.fileno()).st_size > limit:
                    return None
                return fh.read()
        except (OSError, ValueError):
            return None

    def text(self, rel: str) -> str | None:
        if rel in self._bytes:
            self._bytes.move_to_end(rel)
            return self._bytes[rel]
        raw = self.load(rel)
        text = None if raw is None else _decode(raw)
        self._bytes[rel] = text
        while len(self._bytes) > TEXT_CACHE_MAX:
            self._bytes.popitem(last=False)
        return text

    def json(self, rel: str):
        text = self.text(rel)
        if text is None:
            return None
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            return None

    def package_json(self, rel: str):
        if rel in self._pj:
            return self._pj[rel]
        text = self.text(rel)
        data = None
        if text is not None:
            try:
                data = json.loads(text)
            except (ValueError, RecursionError):
                data = None
        if text is None or len(text) < 262144:
            self._pj[rel] = data
        return data

    def followable(self, rel: str) -> bool:
        if rel in self._blocked or any(p in VENDOR_DIRS for p in rel.split("/")[:-1]):
            return False
        path = self.inv.root / rel
        try:
            return path.is_file() and _inside_root(self.inv.root, path)
        except OSError:
            return False

    # -- configuration files: strict, cached per (directory, name), fail closed --

    @staticmethod
    def _dirs(dir_rel: str):
        d = dir_rel
        while True:
            yield d
            if not d:
                return
            d = posixpath.dirname(d)

    def _bad_config(self, rel: str) -> None:
        self.col.add("unknown", rel, None, rel, "config_unreadable", force=True)

    def _listing(self, d: str) -> dict:
        """Entries of directory `d` that are named like a configuration file: name -> "dir" | "file"."""
        if d not in self._dirlist:
            found = {}
            try:
                with os.scandir(self.inv.root / d if d else self.inv.root) as it:
                    for e in it:
                        if e.name in CONFIG_NAMES:
                            found[e.name] = "dir" if e.is_dir(follow_symlinks=False) else "file"
            except OSError:
                pass
            self._dirlist[d] = found
        return self._dirlist[d]

    def probe(self, d: str, fname: str):
        """-> ("absent"|"bad"|"ok", text). Only registry lines are ever used from the text."""
        key = (d, fname)
        if key in self._probe:
            return self._probe[key]
        rel = posixpath.join(d, fname) if d else fname
        result = ("absent", None)
        if rel in self.known:
            result = self._read_config(rel)
        elif self._listing(d).get(fname) == "dir":
            self._bad_config(rel)                      # a directory named like a config file: absent, but noted
        self._probe[key] = result
        return result

    def _read_config(self, rel: str):
        raw = self.load(rel, MAX_CONFIG_BYTES)
        text = None
        if raw is not None:
            try:
                if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                    text = raw.decode("utf-16")
                elif b"\0" in raw:
                    text = None
                else:
                    text = raw.decode("utf-8-sig")
            except UnicodeError:
                text = None
            if text is not None and re.search(r"[\x00-\x08\x0b\x0e-\x1f]", text):
                text = None
        if text is None:
            self._bad_config(rel)
            return ("bad", None)
        return ("ok", text)

    def probe_toml(self, d: str, fname: str):
        key = (d, fname, "toml")
        if key in self._probe:
            return self._probe[key]
        status, text = self.probe(d, fname)
        data = None
        if status == "ok":
            try:
                data = tomllib.loads(text) if tomllib is not None else None
            except (ValueError, RecursionError):
                data = None
            if not isinstance(data, dict):
                data, status = None, "bad"
                self._bad_config(posixpath.join(d, fname) if d else fname)
        result = (status, data)
        self._probe[key] = result
        return result

    def npm_conf(self, dir_rel: str):
        """(default_registry_is_private, {private scopes in lower case}, some config was unreadable)"""
        if dir_rel in self._npm_conf:
            return self._npm_conf[dir_rel]
        default, scopes, possible = _user_npm_state()
        for d in self._dirs(dir_rel):
            if any((posixpath.join(d, f) if d else f) in self.known for f in PNPM_FILES):
                possible = True                          # pnpm files can set registries; they are not parsed
            for fname, parser in ((".npmrc", _parse_npmrc), (".yarnrc.yml", _parse_yarnrc),
                                  (".yarnrc", _parse_yarnrc_classic)):
                status, text = self.probe(d, fname)
                if status == "bad":
                    possible = True
                elif status == "ok":
                    de, sc = parser(text.splitlines())
                    default, scopes = default or de, scopes | sc
        self._npm_conf[dir_rel] = (default, scopes, possible)
        return self._npm_conf[dir_rel]

    def pip_conf(self, dir_rel: str) -> dict:
        if dir_rel in self._pip_conf:
            return dict(self._pip_conf[dir_rel])
        state = {"index_private": False, "extra": False}
        _user_pip_state(state)
        for d in self._dirs(dir_rel):
            for fname in ("pip.conf", "pip.ini"):
                status, text = self.probe(d, fname)
                if status == "bad":
                    state["extra"] = True
                elif status == "ok":
                    _parse_pip_conf(text.splitlines(), state)
            status, text = self.probe(d, "setup.cfg")
            if status == "bad":
                state["extra"] = True
            elif status == "ok":
                _parse_setup_cfg(text.splitlines(), state)
            for fname, parser in (("uv.toml", _parse_uv_toml), ("poetry.toml", _parse_poetry_toml),
                                  ("pyproject.toml", _parse_pyproject_registries), ("Pipfile", _parse_pipfile)):
                status, data = self.probe_toml(d, fname)
                if status == "bad":
                    state["extra"] = True
                elif status == "ok":
                    parser(data, state)
        self._pip_conf[dir_rel] = state
        return dict(state)

    def lock_index(self, rel: str):
        if rel not in self._lock_idx:
            data = self.json(rel)
            self._lock_idx[rel] = _lock_index(data) if isinstance(data, dict) else None
            if self._lock_idx[rel] is None:
                self._bad_config(rel)
        return self._lock_idx[rel]

    def applicable_locks(self, dir_rel: str):
        """Lockfiles of this directory and its ancestors (nearest first): (indexes, some was unreadable)."""
        out, bad = [], False
        for d in self._dirs(dir_rel):
            for fname in ("package-lock.json", "npm-shrinkwrap.json"):
                rel = posixpath.join(d, fname) if d else fname
                if rel in self.known:
                    idx = self.lock_index(rel)
                    if idx is None:
                        bad = True
                    else:
                        out.append(idx)
        return out, bad


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeError:
            pass
    return raw.decode("utf-8-sig", errors="replace")


# ---- registry settings outside the project (environment, user-level files) ---------------------------
# Read-only and fail closed: anything that is not a public registry, or cannot be read, counts as a private
# registry. Tokens and auth lines are never read.

def _env_values(*names) -> list:
    out = []
    for n in names:
        for key in {n, n.lower(), n.upper()}:
            v = os.environ.get(key)
            if v and v.strip():
                out.append(v.strip())
    return out


def _read_user_config(path) -> tuple:
    """-> ("absent"|"bad"|"ok", text) for a file outside the project."""
    try:
        if not path.is_file():
            return ("absent", None)
        with open(path, "rb") as fh:
            raw = fh.read(MAX_CONFIG_BYTES + 1)
    except (OSError, RuntimeError):
        return ("bad", None)
    if len(raw) > MAX_CONFIG_BYTES:
        return ("bad", None)
    try:
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16")
        elif b"\0" in raw:
            text = None
        else:
            text = raw.decode("utf-8-sig")
    except UnicodeError:
        text = None
    if text is None or re.search(r"[\x00-\x08\x0b\x0e-\x1f]", text):
        return ("bad", None)
    return ("ok", text)


def _user_home() -> list:
    try:
        return [Path.home()]
    except (RuntimeError, KeyError, OSError):
        return []


def _user_npm_state() -> tuple:
    """(default_registry_is_private, {private scopes}, some user config was unreadable)"""
    default, scopes, possible = False, set(), False
    if any(not _public(v, PUBLIC_NPM_HOSTS) for v in _env_values("NPM_CONFIG_REGISTRY", "YARN_NPM_REGISTRY_SERVER")):
        default = True
    for key, value in os.environ.items():
        m = re.fullmatch(r"npm_config_(@[A-Za-z0-9][A-Za-z0-9._~-]*):registry", key, re.I)
        if m and not _public(value, PUBLIC_NPM_HOSTS):
            scopes.add(m.group(1).lower())
    paths = [Path(v) for v in _env_values("NPM_CONFIG_USERCONFIG", "NPM_CONFIG_GLOBALCONFIG")]
    paths += [h / ".npmrc" for h in _user_home()]
    for path in paths:
        status, text = _read_user_config(path)
        if status == "bad":
            possible = True
        elif status == "ok":
            de, sc = _parse_npmrc(text.splitlines())
            default, scopes = default or de, scopes | sc
    return default, scopes, possible


def _user_pip_state(state: dict) -> None:
    for v in _env_values("PIP_INDEX_URL", "UV_INDEX_URL", "UV_DEFAULT_INDEX"):
        if not _all_public(v.split(), PUBLIC_PY_HOSTS):
            state["index_private"] = True
    for v in _env_values("PIP_EXTRA_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_INDEX"):
        if not _all_public(v.split(), PUBLIC_PY_HOSTS):
            state["extra"] = True
    if _env_values("PIP_FIND_LINKS", "UV_FIND_LINKS"):
        state["extra"] = True
    paths = [Path(v) for v in _env_values("PIP_CONFIG_FILE") if v != os.devnull]
    for base in _user_home():
        paths += [base / ".config" / "pip" / "pip.conf", base / ".pip" / "pip.conf", base / "pip" / "pip.ini"]
    for var in ("APPDATA", "XDG_CONFIG_HOME"):
        for v in _env_values(var):
            paths += [Path(v) / "pip" / "pip.ini", Path(v) / "pip" / "pip.conf"]
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        status, text = _read_user_config(path)
        if status == "bad":
            state["extra"] = True
        elif status == "ok":
            _parse_pip_conf(text.splitlines(), state)
    for base in _user_home():
        status, text = _read_user_config(base / ".config" / "uv" / "uv.toml")
        if status == "bad":
            state["extra"] = True
        elif status == "ok":
            try:
                data = tomllib.loads(text) if tomllib is not None else None
            except (ValueError, RecursionError):
                data = None
            if isinstance(data, dict):
                _parse_uv_toml(data, state)
            else:
                state["extra"] = True


def _parse_npmrc(lines):
    default, scopes = False, set()
    for line in lines:
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        m = re.match(r"(@[A-Za-z0-9][A-Za-z0-9._~-]*)\s*:\s*registry\s*=\s*(.*)$", s, re.I)
        if m:
            if not _public(m.group(2), PUBLIC_NPM_HOSTS):
                scopes.add(m.group(1).lower())
            continue
        m = re.match(r"registry\s*=\s*(.*)$", s, re.I)
        if m and not _public(m.group(1), PUBLIC_NPM_HOSTS):
            default = True
    return default, scopes


def _parse_yarnrc(lines):
    default, scopes, in_scopes, cur = False, set(), False, None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            in_scopes, cur = bool(re.match(r"npmScopes\s*:", line)), None
            m = re.match(r"npmRegistryServer\s*:\s*(.*)$", line)
            if m and not _public(m.group(1), PUBLIC_NPM_HOSTS):
                default = True
            continue
        if not in_scopes:
            continue
        m = re.match(r"\s+[\"']?([A-Za-z0-9][A-Za-z0-9._~-]*)[\"']?\s*:\s*$", line)
        if m:
            cur = m.group(1).lower()
            continue
        m = re.match(r"\s+npmRegistryServer\s*:\s*(.*)$", line)
        if m and cur and not _public(m.group(1), PUBLIC_NPM_HOSTS):
            scopes.add("@" + cur)
    return default, scopes


def _parse_yarnrc_classic(lines):
    """Yarn 1 .yarnrc: `registry "url"` and `"@scope:registry" "url"`; nothing else is read."""
    default, scopes = False, set()
    for line in lines:
        s = line.strip()
        if not s or s[0] == "#":
            continue
        # the value may be missing (empty): then it is not a public registry
        m = re.match(r"[\"']?(@[A-Za-z0-9][A-Za-z0-9._~-]*):registry[\"']?(?:\s+(\S*)|\s*$)", s, re.I)
        if m:
            if not _public(m.group(2) or "", PUBLIC_NPM_HOSTS):
                scopes.add(m.group(1).lower())
            continue
        m = re.match(r"[\"']?registry[\"']?(?:\s+(\S*)|\s*$)", s, re.I)
        if m and not _public(m.group(1) or "", PUBLIC_NPM_HOSTS):
            default = True
    return default, scopes


_INI_SECTION = re.compile(r"\[([^\]]*)\]")
_INI_OPTION = re.compile(r"([^=:\s][^=:]*?)\s*[=:](.*)$")
_INI_INLINE_COMMENT = re.compile(r"(?:^|\s)[#;].*$")
# setup.cfg sections whose keys are user data (names of extras, entry points, files), never options
_SETUP_CFG_DATA_SECTIONS = frozenset({"metadata", "options.extras_require", "options.entry_points",
                                      "options.package_data", "options.exclude_package_data",
                                      "options.data_files"})


def _ini_values(lines, keys, skip_sections=frozenset()):
    """Yield (key, [value tokens]) for each option in `keys` (lower case, `_` -> `-`) of an INI file.

    Follows configparser: an indented line after an option continues its value; blank lines and
    full-line `#`/`;` comments inside a value are skipped; a section header or unindented line ends
    it. Inline ` #`/` ;` comments are dropped. An empty value gives an empty token list (the caller
    treats that as non-public). Options in `skip_sections` (lower case) are ignored. Linear time.
    """
    section, cur, tokens = None, None, None             # cur: matched key, "" for another option, None
    for line in lines:
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        if line[0].isspace() and cur is not None:
            if tokens is not None:
                tokens.extend(_INI_INLINE_COMMENT.sub("", s).split())
            continue
        if cur:
            yield cur, tokens
        cur, tokens = None, None
        m = _INI_SECTION.match(s)
        if m and not line[0].isspace():
            section = m.group(1).strip().lower()
            continue
        m = _INI_OPTION.match(s)
        if not m:
            continue
        key = m.group(1).strip().lower().replace("_", "-")
        if key in keys and section not in skip_sections:
            cur, tokens = key, _INI_INLINE_COMMENT.sub("", m.group(2).strip()).split()
        else:
            cur = ""
    if cur:
        yield cur, tokens


def _all_public(tokens, hosts) -> bool:
    return bool(tokens) and all(_public(t, hosts) for t in tokens)


def _parse_pip_conf(lines, state):
    """pip.conf / pip.ini, every section (they are all pip command sections); continuation lines followed."""
    for key, tokens in _ini_values(lines, {"index-url", "extra-index-url", "find-links", "trusted-host"}):
        if key == "index-url":
            if not tokens:
                state["extra"] = True                      # empty here: the value may come from elsewhere
            elif not _all_public(tokens, PUBLIC_PY_HOSTS):
                state["index_private"] = True
        elif key == "trusted-host":
            if not tokens or any(_bare_host(t) not in PUBLIC_PY_HOSTS for t in tokens):
                state["extra"] = True
        elif key == "find-links" or not _all_public(tokens, PUBLIC_PY_HOSTS):
            state["extra"] = True


def _bare_host(value: str):
    try:
        return urlsplit("//" + value.strip().strip("\"'")).hostname
    except ValueError:
        return None


def _as_list(value):
    return [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _parse_uv_toml(data, state):
    for table in (data, data.get("pip") if isinstance(data.get("pip"), dict) else {}):
        if any(not _public(u, PUBLIC_PY_HOSTS) for u in _as_list(table.get("index-url"))):
            state["index_private"] = True
        if any(not _public(u, PUBLIC_PY_HOSTS) for u in _as_list(table.get("extra-index-url"))):
            state["extra"] = True
        if table.get("find-links"):
            state["extra"] = True
    for entry in data.get("index", []) if isinstance(data.get("index"), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str) and not _public(entry["url"], PUBLIC_PY_HOSTS):
            state["extra"] = True


def _parse_setup_cfg(lines, state):
    """Only index URL keys are looked at (never credentials as such); data-only sections are skipped."""
    for _key, tokens in _ini_values(lines, {"index-url", "find-links"}, _SETUP_CFG_DATA_SECTIONS):
        if not _all_public(tokens, PUBLIC_PY_HOSTS):
            state["extra"] = True


def _parse_pipfile(data, state):
    sources = data.get("source")
    for entry in sources if isinstance(sources, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str) and \
                not _public(entry["url"], PUBLIC_PY_HOSTS):
            state["extra"] = True


def _parse_pyproject_registries(data, state):
    """Registry settings of a pyproject.toml; they apply to every requirements file and pyproject in scope."""
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    uv = tool.get("uv") if isinstance(tool.get("uv"), dict) else {}
    pip = uv.get("pip") if isinstance(uv.get("pip"), dict) else {}
    for table in (uv, pip):
        if any(not _public(u, PUBLIC_PY_HOSTS) for u in _as_list(table.get("index-url"))):
            state["index_private"] = True
        if any(not _public(u, PUBLIC_PY_HOSTS) for u in _as_list(table.get("extra-index-url"))):
            state["extra"] = True
        if table.get("find-links"):
            state["extra"] = True
    for owner in (tool.get("poetry"), tool.get("pdm"), uv):
        block = owner.get("index" if owner is uv else "source") if isinstance(owner, dict) else None
        for entry in [block] if isinstance(block, dict) else block if isinstance(block, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str) and \
                    not _public(entry["url"], PUBLIC_PY_HOSTS):
                state["extra"] = True


def _parse_poetry_toml(data, state):
    repos = data.get("repositories")
    if isinstance(repos, dict):
        for repo in repos.values():
            if isinstance(repo, dict) and isinstance(repo.get("url"), str) and \
                    not _public(repo["url"], PUBLIC_PY_HOSTS):
                state["extra"] = True


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
    if _public(resolved, PUBLIC_NPM_HOSTS):
        return "public"
    return "git" if _public(resolved, GIT_HOSTS) else "private"


def _lock_made(entry) -> dict:
    cls = _resolved_class(entry.get("resolved"))
    real = entry.get("name")
    return {"version": entry.get("version"), "name": real if isinstance(real, str) else None,
            "local": bool(entry.get("link")) or cls == "git", "private": cls == "private" and not entry.get("link")}


def _lock_index(lock) -> dict:
    """{"top": name -> entry for node_modules/<name>, "all": name -> every entry of that name (nested,
    hoisted, workspace-prefixed)}. Nested copies count as evidence only."""
    top: dict = {}
    allm: dict = {}
    pkgs = lock.get("packages")
    if isinstance(pkgs, dict):
        for key, entry in pkgs.items():
            if not (isinstance(key, str) and "node_modules/" in key and isinstance(entry, dict)):
                continue
            name = key.rsplit("node_modules/", 1)[1]
            made = _lock_made(entry)
            allm.setdefault(name, []).append(made)
            if key == "node_modules/" + name:
                top[name] = made
        return {"top": top, "all": allm}
    stack = [(lock.get("dependencies"), 0)]
    nodes = 0
    while stack and nodes < MAX_LOCK_NODES:
        deps_, depth = stack.pop()
        if not isinstance(deps_, dict):
            continue
        for name, entry in deps_.items():
            nodes += 1
            if not (isinstance(name, str) and isinstance(entry, dict)):
                continue
            made = _lock_made(entry)
            allm.setdefault(name, []).append(made)
            if depth == 0:
                top[name] = made
            stack.append((entry.get("dependencies"), depth + 1))
    return {"top": top, "all": allm}


def _lock_version(entry):
    v = entry.get("version") if entry else None
    m = _SEMVER.fullmatch(v) if isinstance(v, str) else None
    return m.group(1) if m else None


def _npm_private(conf, name) -> str | None:
    default, scopes, possible = conf
    if default or (isinstance(name, str) and name.startswith("@") and name.split("/")[0].lower() in scopes):
        return "private_registry"
    return "private_registry_possible" if possible else None


def _read_npm(ctx, rel):
    col = ctx.col
    data = ctx.package_json(rel)
    if not isinstance(data, dict):
        col.add("unknown", f"(unreadable {rel})", None, rel, "unsupported_manifest", force=True)
        return
    here = posixpath.dirname(rel)
    locks, lock_bad = ctx.applicable_locks(here)
    conf = ctx.npm_conf(here)
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
            real = name
            if reason != "local_or_git" and isinstance(name, str):
                evidence = [e for idx in locks for e in idx["all"].get(name, ())]
                tops = [idx["top"][name] for idx in locks if name in idx["top"]]
                if name in ctx.member_names or any(e["local"] for e in evidence):
                    version, reason = None, "local_or_git"
                elif any(e["private"] for e in evidence):
                    version, reason = None, "private_registry"
                else:
                    if reason in ("unpinned", "alias"):
                        for t in tops:
                            lv = _lock_version(t)
                            if lv:
                                version, reason = lv, None
                                break
                    for t in tops:
                        if t["name"] and t["name"] != name:
                            real = t["name"]
                            break
            if reason is None and lock_bad:
                version, reason = None, "private_registry_possible"
            if reason is None:
                found = _npm_private(conf, real)
                if found:
                    version, reason = None, found
            col.add("npm", real, version, rel, reason)


def _read_npm_lock_only(ctx, rel):
    col = ctx.col
    lock = ctx.json(rel)
    if not isinstance(lock, dict):
        col.add("unknown", f"(unreadable {rel})", None, rel, "unsupported_manifest", force=True)
        return
    conf = ctx.npm_conf(posixpath.dirname(rel))
    idx = _lock_index(lock)
    if col.full:
        col.dropped[rel] += len(idx["top"])
        return
    for key, entry in idx["top"].items():
        name = entry["name"] or key
        version = _lock_version(entry)
        evidence = idx["all"].get(key, ())
        if entry["local"] or any(e["local"] for e in evidence):
            reason = "local_or_git"
        elif entry["private"] or any(e["private"] for e in evidence):
            reason = "private_registry"
        else:
            reason = _npm_private(conf, name) or (None if version else "unpinned")
        col.add("npm", name, version, rel, reason)


# ---- python -------------------------------------------------------------------------

def _requirement(line: str):
    """One PEP 508 style requirement -> (name, version, reason); the name of a spec is a label."""
    if len(line) > MAX_LINE:
        return "<invalid-name>", None, "bad_name"
    line = line.split(";", 1)[0]
    line = _REQ_OPT_CUT.split(line, maxsplit=1)[0].strip()
    if not line:
        return None
    if _pathlike(line) or re.match(r"[^\s=<>!~]+\s*@\s*\S", line):
        return _spec_label(line), None, "local_or_git"
    m = _REQ_NAME.fullmatch(line)
    if not m or not _REQ_REST_OK.match(m.group(3)):
        return "<invalid-name>", None, "bad_name"
    name, rest = m.group(1), m.group(3).strip()
    pin = _REQ_PIN.fullmatch(rest) if rest.startswith("==") else None
    if pin and _safe_version(pin.group(1)):
        return name, pin.group(1), None
    return name, None, "unpinned"


def _req_lines(text: str):
    """Logical lines: backslash continuations joined (before options and pins are parsed), comments
    removed. A joined line over MAX_CONTINUED yields _OVERLONG (the caller fails closed)."""
    buf: list = []
    size = 0
    over = False
    for raw in text.splitlines():
        cont = raw.endswith("\\")
        if not cont and not buf and not over and raw.lstrip().startswith("#"):
            continue
        if not over:
            piece = raw[:-1] if cont else raw
            buf.append(piece)
            size += len(piece)
            if size > MAX_CONTINUED or len(buf) > MAX_CONTINUATIONS:
                over, buf = True, []
        if cont:
            continue
        if over:
            yield _OVERLONG
            over, size = False, 0
            continue
        line = "".join(buf)
        buf, size = [], 0
        line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
        if line:
            yield line
    if over:
        yield _OVERLONG
    elif buf:
        line = re.split(r"(?:^|\s)#", "".join(buf), maxsplit=1)[0].strip()
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
    over_labels = 0
    seen: set = set()

    def pending() -> int:
        return len(col.items) + len(deps_found)

    def unsupported_add(label):
        nonlocal over_labels
        if len(unsupported) >= MAX_UNSUPPORTED_LABELS:
            over_labels += 1
        else:
            unsupported.append(label)

    def visit(rel: str, depth: int):
        if rel in seen:
            return
        seen.add(rel)
        text = ctx.text(rel)
        if text is None:
            unsupported_add(_label(rel))
            return
        for line in _req_lines(text):
            if line is _OVERLONG:
                state["extra"] = True
                deps_found.append(("<invalid-name>", None, "bad_name", rel))
                continue
            if line[0] == "-":
                m = _INCLUDE.match(line)
                if m:
                    resolved = _resolve_include(rel, m.group(2))
                    is_req = m.group(1) in ("-r", "--requirement")
                    if resolved is not None and resolved in seen:
                        continue
                    if is_req and resolved is not None and depth < MAX_INCLUDE_DEPTH:
                        if ctx.follows >= MAX_INCLUDES_FOLLOWED:
                            unsupported_add("(include limit)")
                        elif ctx.followable(resolved):
                            ctx.follows += 1
                            visit(resolved, depth + 1)
                        else:
                            unsupported_add(_label(resolved))
                    else:
                        unsupported_add(_label(resolved))
                    continue
                m = _INDEX_URL.match(line)
                if m:
                    if not _public(m.group(1), PUBLIC_PY_HOSTS):
                        state["index_private"] = True       # sticky: a later public URL never resets it
                    continue
                m = _EXTRA_INDEX.match(line)
                if m:
                    if not _public(m.group(1), PUBLIC_PY_HOSTS):
                        state["extra"] = True
                    continue
                if _FIND_LINKS.match(line):
                    state["extra"] = True
                    continue
                m = _TRUSTED_HOST.match(line)
                if m:
                    if _bare_host(m.group(1)) not in PUBLIC_PY_HOSTS:
                        state["extra"] = True
                    continue
                if _EDITABLE.match(line):
                    if pending() >= MAX_DEPENDENCIES:
                        col.dropped[rel] += 1
                    else:
                        deps_found.append((_spec_label(line), None, "local_or_git", rel))
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
    if over_labels:
        col.add("unknown", "(include limit)", None, "(include limit)", "unsupported_manifest", force=True,
                count=over_labels)
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
    pip = uv.get("pip") if isinstance(uv.get("pip"), dict) else {}
    for table in (uv, pip):
        for key in ("index-url", "extra-index-url"):
            urls.extend(_as_list(table.get(key)))
    private = any(not _public(u, PUBLIC_PY_HOSTS) for u in urls) or bool(uv.get("find-links") or pip.get("find-links"))
    names = {e["name"]: _public(e.get("url"), PUBLIC_PY_HOSTS) for e in entries
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
    _private, index_public = _pyproject_sources(data)
    conf = ctx.pip_conf(posixpath.dirname(rel))          # includes this file and all ancestors' settings
    verdict = ("private_registry" if conf["index_private"] else
               "private_registry_possible" if conf["extra"] else None)
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
    for m in inv.manifests:
        if m.kind == "package.json":
            data = ctx.package_json(m.rel)
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                ctx.member_names.add(data["name"])
    consumed: set = set()
    for m in inv.manifests:
        prefix = posixpath.dirname(m.rel)
        prefix = prefix + "/" if prefix else ""
        if m.kind == "package.json":
            _read_npm(ctx, m.rel)
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
    def n_of(n, one, many):
        return f"{n} {one if n == 1 else many}"

    warn = " Scoped packages are sent by name. If this scope is your organisation's private scope, strike this request."
    groups = [(c, n_of(len(c), "unscoped dependency", "unscoped dependencies"), "") for c in _batches(unscoped)]
    for s in own:
        groups += [(b, f"{n_of(len(b), 'package', 'packages')} of scope {s}", warn) for b in _batches(by_scope[s])]
    merged = sorted(c for s in rest for c in by_scope[s])
    groups += [(b, f"{n_of(len(b), 'package', 'packages')} of other scopes",
                warn.replace("this scope is your organisation's private scope", "any of these scopes is private"))
               for b in _batches(merged)]
    requests, mapping = [], {}
    for n, (batch, what, warning) in enumerate(groups, 1):
        rid = f"cd-{n}"
        requests.append({
            "id": rid, "method": "POST", "url": CD_URL,
            "body": json.dumps(batch, separators=(",", ":")),
            "headers": {"Content-Type": "application/json"},
            "purpose": f"licence lookup for {what} (name and version only).{warning}",
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
    "private_registry": "dependencies resolved from a private registry or index were not sent; mirrors and "
                        "proxies of public registries cannot be told apart from private ones and are excluded too "
                        "(fails closed)",
    "private_registry_possible": "the project configures an extra package index (or has pnpm workspace files, "
                                 "which can set registries), so the source of each package cannot be told; none "
                                 "of those dependencies was sent",
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
    "config_unreadable": "a configuration file could not be read (too large, binary, not valid text or TOML, or a "
                         "directory); dependencies in its scope were treated as private_registry_possible",
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
