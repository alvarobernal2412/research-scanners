"""Find a project's dependencies from manifests and lockfiles. Local only, no network."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "env", "__pycache__", "site-packages", "dist", "build",
             "target", ".tox", ".mypy_cache", ".pytest_cache", "vendor"}
MAX_DEPTH = 4
DEV_WORDS = re.compile(r"(^|[-_/.])(dev|test|tests|testing|lint|docs?|typing|ci|format|check|build)([-_/.]|$)", re.I)
NAME_RX = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def pynorm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dep(ecosystem, name, version, scope, direct, source, origin, spec=None, license_hint=None) -> dict:
    return {"ecosystem": ecosystem, "name": name, "version": version, "scope": scope, "direct": direct,
            "source": source, "spec": spec, "origin": origin, "license_hint": license_hint}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---- Python ------------------------------------------------------------------------------

def parse_requirement(line: str, scope: str, origin: str) -> dict | None:
    line = line.split(" #")[0].strip()
    if not line or line.startswith(("#", "-r", "-c", "--")):
        return None
    if line.startswith(("-e", "git+", "hg+", "svn+", "bzr+", "http://", "https://", "file:", ".", "/")) or " @ " in line:
        egg = re.search(r"[#&]egg=([A-Za-z0-9._-]+)", line)
        direct_ref = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s+@\s", line)
        if egg:
            name = egg.group(1)
        elif direct_ref:
            name = direct_ref.group(1)
        else:
            name = line.replace("-e", "", 1).strip()
        kind = "git" if "git" in line.lower() else ("path" if line.startswith(("-e", ".", "/", "file:")) else "url")
        return dep("pypi", name, None, scope, True, kind, origin, spec=line)
    m = NAME_RX.match(line)
    if not m:
        return None
    name = m.group(1)
    rest = line[m.end():]
    rest = re.sub(r"^\[[^\]]*\]", "", rest).split(";")[0].strip()
    pin = re.match(r"^===?\s*([A-Za-z0-9.!+_*-]+)\s*$", rest)
    version = pin.group(1) if pin and "*" not in pin.group(1) else None
    return dep("pypi", name, version, scope, True, "registry", origin, spec=rest or None)


def parse_requirements_txt(path: Path, root: Path, seen: set | None = None) -> list[dict]:
    seen = seen or set()
    if path in seen:
        return []
    seen.add(path)
    scope = "dev" if DEV_WORDS.search(path.stem) else "runtime"
    origin = path.relative_to(root).as_posix()
    out = []
    for raw in _read(path).splitlines():
        s = raw.strip()
        if s.startswith("-r "):
            inc = (path.parent / s[3:].strip()).resolve()
            if inc.is_file():
                out += parse_requirements_txt(inc, root, seen)
            continue
        d = parse_requirement(raw, scope, origin)
        if d:
            out.append(d)
    return out


def _toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:
        return {}
    try:
        return tomllib.loads(_read(path))
    except Exception:
        return {}


def parse_pyproject(path: Path, root: Path) -> list[dict]:
    data = _toml(path)
    origin = path.relative_to(root).as_posix()
    out = []
    proj = data.get("project", {})
    for line in proj.get("dependencies", []) or []:
        d = parse_requirement(line, "runtime", origin)
        if d:
            out.append(d)
    for group, lines in (proj.get("optional-dependencies", {}) or {}).items():
        scope = "dev" if DEV_WORDS.search(group) else "runtime"
        for line in lines:
            d = parse_requirement(line, scope, origin)
            if d:
                d["optional_extra"] = group
                out.append(d)
    for group, lines in (data.get("dependency-groups", {}) or {}).items():
        for line in lines:
            if isinstance(line, str):
                d = parse_requirement(line, "dev" if DEV_WORDS.search(group) else "runtime", origin)
                if d:
                    out.append(d)
    poetry = data.get("tool", {}).get("poetry", {})

    def poetry_deps(table: dict, scope: str):
        for name, spec in (table or {}).items():
            if name.lower() == "python":
                continue
            source, version = "registry", None
            if isinstance(spec, dict):
                if "git" in spec:
                    source = "git"
                elif "path" in spec:
                    source = "path"
                elif "url" in spec:
                    source = "url"
                spec_s = spec.get("version")
            else:
                spec_s = spec
            if isinstance(spec_s, str) and re.fullmatch(r"=?=?\s*\d[\w.]*", spec_s):
                version = spec_s.lstrip("= ")
            out.append(dep("pypi", name, version, scope, True, source, origin, spec=str(spec_s) if spec_s else None))

    poetry_deps(poetry.get("dependencies"), "runtime")
    poetry_deps(poetry.get("dev-dependencies"), "dev")
    for gname, g in (poetry.get("group", {}) or {}).items():
        poetry_deps(g.get("dependencies"), "dev" if DEV_WORDS.search(gname) or gname != "main" else "runtime")
    return out


def parse_pipfile_lock(path: Path, root: Path) -> list[dict]:
    try:
        data = json.loads(_read(path))
    except json.JSONDecodeError:
        return []
    origin = path.relative_to(root).as_posix()
    out = []
    for section, scope in (("default", "runtime"), ("develop", "dev")):
        for name, info in (data.get(section) or {}).items():
            ver = (info.get("version") or "").lstrip("=") or None
            source = "registry" if "version" in info else ("git" if "git" in info else "path")
            out.append(dep("pypi", name, ver, scope, False, source, origin))
    return out


def parse_poetry_lock(path: Path, root: Path) -> list[dict]:
    data = _toml(path)
    origin = path.relative_to(root).as_posix()
    out = []
    for p in data.get("package", []) or []:
        groups = p.get("groups")
        if groups is not None:
            scope = "runtime" if "main" in groups else "dev"
        else:
            scope = "dev" if p.get("category") == "dev" else "runtime"
        src = (p.get("source") or {}).get("type", "registry")
        source = {"legacy": "registry", "registry": "registry", "git": "git", "directory": "path",
                  "file": "path", "url": "url"}.get(src, "registry")
        out.append(dep("pypi", p["name"], p.get("version"), scope, False, source, origin))
    return out


# ---- npm ---------------------------------------------------------------------------------

NPM_NONREG = re.compile(r"^(git\+|git://|github:|gitlab:|bitbucket:|https?://|file:|link:|\.{1,2}/|/|[\w.-]+/[\w.-]+$)")


def _npm_source(spec: str) -> str:
    if spec.startswith(("npm:", "workspace:")):
        return "registry" if spec.startswith("npm:") else "path"
    if spec.startswith(("file:", "link:", ".", "/")):
        return "path"
    if NPM_NONREG.match(spec):
        return "git" if not spec.startswith("http") else "url"
    return "registry"


def parse_package_json(path: Path, root: Path) -> list[dict]:
    try:
        data = json.loads(_read(path))
    except json.JSONDecodeError:
        return []
    origin = path.relative_to(root).as_posix()
    out = []
    for key, scope in (("dependencies", "runtime"), ("optionalDependencies", "runtime"),
                       ("peerDependencies", "runtime"), ("devDependencies", "dev")):
        for name, spec in (data.get(key) or {}).items():
            spec = str(spec)
            version = spec if re.fullmatch(r"\d+\.\d+\.\d+([-+][\w.]+)?", spec) else None
            out.append(dep("npm", name, version, scope, True, _npm_source(spec), origin, spec=spec))
    return out


def parse_package_lock(path: Path, root: Path) -> list[dict]:
    try:
        data = json.loads(_read(path))
    except json.JSONDecodeError:
        return []
    origin = path.relative_to(root).as_posix()
    out = []
    pkgs = data.get("packages")
    if isinstance(pkgs, dict):
        for key, info in pkgs.items():
            if not key or info.get("link"):
                continue
            name = info.get("name") or key.rsplit("node_modules/", 1)[-1]
            resolved = info.get("resolved") or ""
            source = "registry"
            if resolved.startswith(("git+", "git:")) or "github.com" in resolved and "codeload" not in resolved and "/-/" not in resolved:
                source = "git"
            elif resolved.startswith("file:"):
                source = "path"
            lic = info.get("license")
            if isinstance(lic, (dict, list)):
                lic = json.dumps(lic)
            out.append(dep("npm", name, info.get("version"), "dev" if info.get("dev") else "runtime",
                           False, source, origin, license_hint=lic))
    else:  # lockfile v1
        def walk(deps: dict):
            for name, info in (deps or {}).items():
                resolved = info.get("resolved") or ""
                source = "git" if resolved.startswith("git") else "registry"
                out.append(dep("npm", name, info.get("version"), "dev" if info.get("dev") else "runtime",
                               False, source, origin))
                walk(info.get("dependencies"))
        walk(data.get("dependencies"))
    return out


# ---- collection --------------------------------------------------------------------------

def _find(root: Path, pattern_test):
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            children = sorted(d.iterdir())
        except OSError:
            continue
        for c in children:
            if c.is_dir():
                if c.name not in SKIP_DIRS and not c.name.startswith(".") and depth < MAX_DEPTH:
                    stack.append((c, depth + 1))
            elif pattern_test(c.name):
                yield c


def collect(root: Path) -> dict:
    root = Path(root).resolve()
    manifests: list[dict] = []
    found: list[dict] = []
    lockfiles = {"pypi": False, "npm": False}

    def add(path: Path, kind: str, deps: list[dict]):
        manifests.append({"file": path.relative_to(root).as_posix(), "kind": kind, "dependencies": len(deps)})
        found.extend(deps)

    for p in _find(root, lambda n: re.fullmatch(r"requirements([-_.][\w.-]*)?\.(txt|in)", n) is not None):
        if p.suffix == ".in" and p.with_suffix(".txt").exists():
            continue
        add(p, "requirements", parse_requirements_txt(p, root))
    for p in _find(root, lambda n: n == "pyproject.toml"):
        add(p, "pyproject", parse_pyproject(p, root))
    for p in _find(root, lambda n: n == "Pipfile.lock"):
        lockfiles["pypi"] = True
        add(p, "Pipfile.lock", parse_pipfile_lock(p, root))
    for p in _find(root, lambda n: n == "poetry.lock"):
        lockfiles["pypi"] = True
        add(p, "poetry.lock", parse_poetry_lock(p, root))
    for p in _find(root, lambda n: n == "package.json"):
        add(p, "package.json", parse_package_json(p, root))
    for p in _find(root, lambda n: n in ("package-lock.json", "npm-shrinkwrap.json")):
        lockfiles["npm"] = True
        add(p, "package-lock.json", parse_package_lock(p, root))

    # Pinned requirements.txt entries count as a lockfile only when every entry is pinned.
    req_deps = [d for d in found if d["ecosystem"] == "pypi" and "requirements" in d["origin"]]
    if req_deps and all(d["version"] or d["source"] != "registry" for d in req_deps):
        lockfiles["pypi"] = True

    merged = _merge(found)
    notes = []
    for eco, label in (("pypi", "Python"), ("npm", "npm")):
        has_deps = any(d["ecosystem"] == eco for d in merged)
        if has_deps and not lockfiles[eco]:
            notes.append(f"No {label} lockfile or fully pinned requirements: only direct dependencies are known, "
                         "transitive dependencies were not checked.")
    return {"root": str(root), "manifests": manifests, "dependencies": merged, "lockfile_present": lockfiles,
            "notes": notes}


def _key(d: dict):
    name = pynorm(d["name"]) if d["ecosystem"] == "pypi" else d["name"]
    return d["ecosystem"], name


def _merge(found: list[dict]) -> list[dict]:
    """Merge duplicates. Direct flag comes from manifests, version from lockfiles."""
    direct_names = {_key(d) for d in found if d["direct"]}
    by_key: dict[tuple, dict] = {}
    for d in found:
        k = _key(d) + (d["version"],)
        cur = by_key.get(k)
        if cur is None:
            by_key[k] = dict(d)
        else:
            if d["scope"] == "runtime":
                cur["scope"] = "runtime"
            cur["license_hint"] = cur.get("license_hint") or d.get("license_hint")
    out = list(by_key.values())
    # Drop an unpinned manifest entry when a lockfile pins the same package.
    pinned = {_key(d) for d in out if d["version"] and d["source"] == "registry"}
    out = [d for d in out if d["version"] or d["source"] != "registry" or _key(d) not in pinned]
    for d in out:
        d["direct"] = _key(d) in direct_names
    out.sort(key=lambda d: (d["ecosystem"], d["name"].lower(), d["version"] or ""))
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(collect(Path(sys.argv[1] if len(sys.argv) > 1 else ".")), indent=2))
