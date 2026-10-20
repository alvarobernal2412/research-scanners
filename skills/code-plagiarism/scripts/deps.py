"""Strict tier: declared dependencies and their licences from ClearlyDefined.

Only public package coordinates (for example npm/npmjs/-/is-odd/3.0.1) leave the machine. They
come from the project's own manifests (package.json, package-lock.json, requirements*.txt,
pyproject.toml); manifests inside vendored directories belong to the dependencies themselves and
are never read. Only direct, declared dependencies with an exact version are looked up.

`plan` writes the query file (one POST per CD_BATCH coordinates); `run` sends only what an
approved plan lists. Names that do not look like public package names (paths, URLs, odd
characters) are excluded with a reason and never sent. The plan's `excluded` section holds
counts per manifest, never names.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import net  # noqa: E402
from approval import ApprovalError, Guard, build_plan, load_plan, render_preview, write_plan  # noqa: E402
from inventory import Inventory, InventoryError, inventory  # noqa: E402
from model import new_results, write_results  # noqa: E402
from replay import RecordingOpener  # noqa: E402

try:
    import tomllib
except ImportError:                                  # Python < 3.11
    tomllib = None

CD_URL = "https://api.clearlydefined.io/definitions"
CD_BATCH = 50
CD_QUOTA = "5000 per 5 minutes"
MAX_DEPENDENCIES = 5000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_LIST = 50
MAX_TEXT = 300

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


@dataclass
class Dependency:
    ecosystem: str          # npm | pypi (unknown for an unsupported manifest)
    name: str
    version: str | None
    manifest: str
    excluded_reason: str | None   # unpinned|local_or_git|unsupported_manifest|no_tomllib|bad_name|too_many


# ---- helpers ------------------------------------------------------------------------

def _pathlike(text: str) -> bool:
    low = text.lower()
    return (low.startswith(_LOCAL_PREFIXES) or text[:1] in (".", "/", "\\") or text.startswith(("~/", "~\\")) or "://" in text
            or bool(_WIN_DRIVE.match(text)))


def _safe_name(ecosystem: str, name) -> bool:
    if not isinstance(name, str):
        return False
    pat = _NPM_NAME if ecosystem == "npm" else _PYPI_NAME
    return len(name) <= 214 and pat.fullmatch(name) is not None


def _safe_version(version) -> bool:
    return isinstance(version, str) and _VERSION.fullmatch(version) is not None


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
    return f"pypi/pypi/-/{dep.name.lower().replace('_', '-')}/{dep.version}"


def _short(text) -> str:
    return str(text)[:200]


class _Collector:
    def __init__(self):
        self.items: list[Dependency] = []
        self.full = False
        self.last_manifest = ""

    def add(self, ecosystem, name, version, manifest, reason=None) -> bool:
        if self.full:
            return False
        self.last_manifest = manifest
        if len(self.items) >= MAX_DEPENDENCIES:
            self.full = True
            return False
        if reason is None and (not _safe_name(ecosystem, name) or (version is not None and not _safe_version(version))):
            reason = "local_or_git" if isinstance(name, str) and _pathlike(name) else "bad_name"
        self.items.append(Dependency(ecosystem, _short(name), version if reason is None else None, manifest, reason))
        return True

    def finish(self) -> list[Dependency]:
        if self.full:
            self.items.append(Dependency("unknown", "(more dependencies)", None, self.last_manifest, "too_many"))
        return self.items


def _load_bytes(inv: Inventory, rel: str) -> bytes | None:
    path = inv.root / rel
    try:
        if not path.resolve().is_relative_to(inv.root):
            return None
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        return path.read_bytes()
    except (OSError, ValueError):
        return None


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeError:
            pass
    return raw.decode("utf-8-sig", errors="replace")


def _load_json(inv: Inventory, rel: str):
    raw = _load_bytes(inv, rel)
    if raw is None:
        return None
    try:
        return json.loads(_decode(raw))
    except (ValueError, RecursionError):
        return None


# ---- npm ----------------------------------------------------------------------------

def _npm_spec(spec):
    """-> (version | None, reason | None)"""
    if not isinstance(spec, str):
        return None, "unpinned"
    s = spec.strip()
    if s.lower().startswith("npm:"):
        return None, "unpinned"                      # registry alias: the real name is not the key
    if _pathlike(s) or "/" in s:
        return None, "local_or_git"
    m = _SEMVER.fullmatch(s[1:] if s.startswith("=") else s)
    return (m.group(1), None) if m else (None, "unpinned")


def _lock_entries(lock) -> dict:
    """name -> {"version": ..., "local": bool} for the top-level packages of a lockfile."""
    out: dict = {}
    if not isinstance(lock, dict):
        return out
    pkgs = lock.get("packages")
    if isinstance(pkgs, dict):
        for key, entry in pkgs.items():
            if not (isinstance(key, str) and key.startswith("node_modules/") and isinstance(entry, dict)):
                continue
            name = key[len("node_modules/"):]
            if "node_modules/" in name:
                continue                              # nested: a transitive copy
            resolved = entry.get("resolved")
            local = bool(entry.get("link")) or (isinstance(resolved, str) and _pathlike(resolved)
                                                and not resolved.startswith(("http:", "https:")))
            out[name] = {"version": entry.get("version"), "local": local}
        return out
    old = lock.get("dependencies")
    if isinstance(old, dict):
        for name, entry in old.items():
            if isinstance(entry, dict) and isinstance(name, str):
                out[name] = {"version": entry.get("version"), "local": False}
    return out


def _lock_version(entry):
    v = entry.get("version") if entry else None
    m = _SEMVER.fullmatch(v) if isinstance(v, str) else None
    return m.group(1) if m else None


def _read_npm(inv, col, rel, lock_rel):
    data = _load_json(inv, rel)
    if not isinstance(data, dict):
        col.add("unknown", f"(unreadable {rel})", None, rel, "unsupported_manifest")
        return
    lock = _lock_entries(_load_json(inv, lock_rel)) if lock_rel else {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            version, reason = _npm_spec(spec)
            if reason is None:
                pass                                   # an exact version in package.json
            elif reason == "unpinned" and isinstance(name, str) and name in lock:
                entry = lock[name]
                if entry["local"]:
                    reason = "local_or_git"
                else:
                    version = _lock_version(entry)
                    reason = None if version else "unpinned"
            if isinstance(name, str) and name in lock and lock[name]["local"]:
                version, reason = None, "local_or_git"
            if not col.add("npm", name, version, rel, reason):
                return


def _read_npm_lock_only(inv, col, rel):
    lock = _load_json(inv, rel)
    entries = _lock_entries(lock)
    if not isinstance(lock, dict):
        col.add("unknown", f"(unreadable {rel})", None, rel, "unsupported_manifest")
        return
    for name, entry in entries.items():
        version = _lock_version(entry)
        reason = "local_or_git" if entry["local"] else (None if version else "unpinned")
        if not col.add("npm", name, version, rel, reason):
            return


# ---- python -------------------------------------------------------------------------

def _requirement(col, line: str, rel: str) -> bool:
    """Handle one PEP 508 style requirement. Returns False when the collector is full."""
    line = line.split(";", 1)[0].strip()
    if not line:
        return True
    if _pathlike(line) or re.match(r"[^\s=<>!~]+\s*@\s*\S", line):
        return col.add("pypi", line, None, rel, "local_or_git")
    m = _REQ_NAME.fullmatch(line)
    if not m or not _REQ_REST_OK.match(m.group(3)):
        return col.add("pypi", line, None, rel, "bad_name")
    name, rest = m.group(1), m.group(3).strip()
    pin = _REQ_PIN.fullmatch(rest) if rest.startswith("==") else None
    if pin and _safe_version(pin.group(1)):
        return col.add("pypi", name, pin.group(1), rel, None)
    return col.add("pypi", name, None, rel, "unpinned")


def _read_requirements(inv, col, rel):
    raw = _load_bytes(inv, rel)
    if raw is None:
        col.add("unknown", f"(unreadable {rel})", None, rel, "unsupported_manifest")
        return
    for line in _decode(raw).splitlines():
        line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip().rstrip("\\").strip()
        if not line:
            continue
        if line.startswith("-"):
            if re.match(r"(?:-e|--editable)(?:\s|=|$)", line):
                if not col.add("pypi", line, None, rel, "local_or_git"):
                    return
            continue                                   # -r, -c, --hash, --index-url ...
        if not _requirement(col, line, rel):
            return


def _poetry_block(col, block, rel):
    if not isinstance(block, dict):
        return True
    for name, spec in block.items():
        if name == "python":
            continue
        if isinstance(spec, dict):
            if any(k in spec for k in ("git", "path", "url")):
                ok = col.add("pypi", name, None, rel, "local_or_git")
            else:
                ok = _poetry_spec(col, name, spec.get("version"), rel)
        else:
            ok = _poetry_spec(col, name, spec, rel)
        if not ok:
            return False
    return True


def _poetry_spec(col, name, spec, rel):
    if isinstance(spec, str) and re.fullmatch(r"\d+(?:\.\d+)*(?:[A-Za-z0-9.+-]*)", spec.strip()) \
            and _safe_version(spec.strip()):
        return col.add("pypi", name, spec.strip(), rel, None)
    return col.add("pypi", name, None, rel, "unpinned")


def _read_pyproject(inv, col, rel):
    if tomllib is None:
        col.add("pypi", "(pyproject.toml)", None, rel, "no_tomllib")
        return
    raw = _load_bytes(inv, rel)
    try:
        data = tomllib.loads(_decode(raw)) if raw is not None else None
    except (tomllib.TOMLDecodeError, RecursionError, ValueError):
        data = None
    if not isinstance(data, dict):
        col.add("unknown", f"(unreadable {rel})", None, rel, "unsupported_manifest")
        return
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
        for item in items:
            if isinstance(item, str):
                if not _requirement(col, item.strip(), rel):
                    return
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    for key in ("dependencies", "dev-dependencies"):
        if not _poetry_block(col, poetry.get(key), rel):
            return
    pgroups = poetry.get("group")
    if isinstance(pgroups, dict):
        for g in pgroups.values():
            if isinstance(g, dict) and not _poetry_block(col, g.get("dependencies"), rel):
                return


# ---- public API ---------------------------------------------------------------------

def read_dependencies(inv: Inventory) -> list[Dependency]:
    """Declared dependencies from the project's own manifests (never from vendored directories)."""
    col = _Collector()
    rels = {m.rel for m in inv.manifests}
    for m in inv.manifests:
        if col.full:
            break
        base, _, name = m.rel.rpartition("/")
        prefix = base + "/" if base else ""
        if m.kind == "package.json":
            lock = prefix + "package-lock.json"
            _read_npm(inv, col, m.rel, lock if lock in rels else None)
        elif m.kind == "package-lock.json":
            if prefix + "package.json" not in rels:
                _read_npm_lock_only(inv, col, m.rel)
        elif m.kind == "requirements":
            _read_requirements(inv, col, m.rel)
        elif m.kind == "pyproject":
            _read_pyproject(inv, col, m.rel)
        else:
            col.add("unknown", m.rel, None, m.rel, "unsupported_manifest")
    return col.finish()


def make_plan(root: Path) -> dict:
    inv = inventory(Path(root), require_sources=False)
    found = read_dependencies(inv)
    manifests: list[str] = []
    coords: dict[str, list[int]] = {}
    excluded: Counter = Counter()
    for dep in found:
        if dep.manifest not in manifests:
            manifests.append(dep.manifest)
        idx = manifests.index(dep.manifest)
        c = coordinate(dep)
        if c is None:
            excluded[(dep.manifest, dep.excluded_reason or "bad_name")] += 1
        else:
            where = coords.setdefault(c, [])
            if idx not in where:
                where.append(idx)
    ordered = sorted(coords)
    requests, mapping = [], {}
    for n, start in enumerate(range(0, len(ordered), CD_BATCH), 1):
        batch = ordered[start:start + CD_BATCH]
        rid = f"cd-{n}"
        requests.append({
            "id": rid, "method": "POST", "url": CD_URL,
            "body": json.dumps(batch, separators=(",", ":")),
            "headers": {"Content-Type": "application/json"},
            "purpose": f"licence lookup for {len(batch)} declared dependencies (name and version only)",
            "source": None,
        })
        mapping[rid] = [coords[c] for c in batch]
    n = len(requests)
    disclosure = [
        "Sent: the package name and exact version of declared dependencies, as public package "
        "coordinates (for example npm/npmjs/-/name/1.2.3), to api.clearlydefined.io.",
        "A private or internal package that happens to have an exact version would be sent by name: "
        "read the request bodies above and strike any such request before approving.",
        "Not sent: file paths, file contents, the project name, dependencies with ranges or local/git specs.",
        "Only direct dependencies declared in the project's own manifests are looked up; transitive "
        "dependencies are not.",
    ]
    return build_plan(
        backend="clearlydefined", tier="strict", root=str(inv.root), requests=requests, followups=[],
        disclosure=disclosure, cost={"requests": n, "quota": f"{n} of {CD_QUOTA}"},
        excluded=[{"rel": rel, "reason": f"{reason} ({count})"} for (rel, reason), count in sorted(excluded.items())],
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
    """coordinate -> {declared, discovered, attribution, source_url, release_date, status, confidence, known}"""
    out: dict = {}
    if not isinstance(body, dict):
        return out
    for coord, d in body.items():
        if not isinstance(coord, str) or not isinstance(d, dict):
            continue
        eff = _dig(d, "scores", "effective")
        eff = eff if isinstance(eff, (int, float)) and not isinstance(eff, bool) else 0
        known = eff > 0
        declared = _text(_dig(d, "licensed", "declared"))
        if declared is not None and declared.upper() in ("", "NOASSERTION", "OTHER"):
            declared = None
        status = "unknown" if not known else ("declared" if declared else "none_found")
        out[coord] = {
            "declared": declared if known else None,
            "discovered": _str_list(_dig(d, "licensed", "facets", "core", "discovered", "expressions")),
            "attribution": _str_list(_dig(d, "licensed", "facets", "core", "attribution", "parties")),
            "source_url": _text(_dig(d, "described", "sourceLocation", "url")),
            "release_date": _text(_dig(d, "described", "releaseDate")),
            "status": status,
            "confidence": round(min(eff, 100) / 100, 2),
            "known": known,
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


def run(plan_path: Path, approved_sha256: str, opener=None, sleep=time.sleep) -> dict:
    plan = load_plan(plan_path, approved_sha256)          # raises ApprovalError before anything is sent
    guard = Guard(plan)
    res = new_results("clearlydefined", "strict", plan["sha256"], plan["root"])
    local = plan["local_only"]
    manifests = local.get("manifests", []) if isinstance(local.get("manifests"), list) else []
    mapping = local.get("coordinate_manifests", {}) if isinstance(local.get("coordinate_manifests"), dict) else {}
    deps_out: list = []

    def entry(coord, idxs, parsed):
        eco, name, version = _parse_coordinate(coord) or ("npm", coord, None)
        names = [manifests[i] for i in idxs if isinstance(i, int) and 0 <= i < len(manifests)]
        got = parsed
        lic = {"spdx": got["declared"] if got else None,
               "status": got["status"] if got else "unknown",
               "evidence": "clearlydefined" if got else None,
               "confidence": got["confidence"] if got else None}
        item = {"ecosystem": eco, "name": name, "version": version, "coordinate": coord,
                "manifest": names[0] if names else None, "manifests": names,
                "license": lic, "known": bool(got and got["known"])}
        if got:
            item.update({k: got[k] for k in ("discovered", "attribution", "source_url", "release_date")})
        return item

    stopped = False
    for req in plan["requests"]:
        coords = json.loads(req["body"])
        maps = mapping.get(req["id"], [])
        maps = maps if isinstance(maps, list) else []

        def not_sent(reason):
            res["not_sent"].append({"id": req["id"], "method": req["method"], "url": req["url"],
                                    "coordinates": len(coords), "reason": reason})
            for i, c in enumerate(coords):
                deps_out.append(entry(c, maps[i] if i < len(maps) and isinstance(maps[i], list) else [], None))

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
            deps_out.append(entry(c, maps[i] if i < len(maps) and isinstance(maps[i], list) else [], got))
        if missing:
            res["errors"].append(_error("incomplete_response", f"{missing} coordinates had no definition",
                                        req["id"]))
    res["dependencies"] = deps_out
    res["requests_sent"] = list(guard.sent)
    res["notes"].append("Direct declared dependencies with an exact version only; transitive dependencies "
                        "are not looked up.")
    for e in plan["excluded"]:
        res["notes"].append(f"not looked up: {e.get('rel')}: {e.get('reason')}")
    return res


# ---- CLI ----------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": "usage", "detail": message}))
        raise SystemExit(2)


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
            write_plan(plan, Path(args.out))
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
        write_results(Path(args.out), res)
        if res["errors"]:
            first = res["errors"][0]
            print(json.dumps({"error": first["code"], "detail": first["detail"], "errors": len(res["errors"]),
                              "not_sent": len(res["not_sent"]), "results_path": str(Path(args.out).resolve())}))
            return 2
        print(json.dumps({"results_path": str(Path(args.out).resolve()), "dependencies": len(res["dependencies"]),
                          "known": sum(1 for d in res["dependencies"] if d["known"]),
                          "requests_sent": len(res["requests_sent"])}))
        return 0
    except ApprovalError as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}))
    except InventoryError as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}))
    except OSError:
        print(json.dumps({"error": "output_error", "detail": "file system error"}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
