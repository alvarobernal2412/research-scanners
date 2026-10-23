"""Find each dependency's license: lockfile hint, local metadata, then PyPI / npm.

Privacy: the only data ever sent is a registry package name and version, in the URL path of a
PyPI or npm registry lookup. Dependencies from git, path or URL sources are never sent (their
names may be private). Every request is recorded and returned to the caller.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import licenses  # noqa: E402

USER_AGENT = "license-compliance-skill/1.0 (package license lookup; only package names and versions are sent)"
PYPI = "https://pypi.org/pypi/"
NPM = "https://registry.npmjs.org/"
TIMEOUT = 15
MAX_RETRIES = 2
MAX_RETRY_AFTER = 10
REQUEST_DELAY = 0.05
VENV_DIRS = (".venv", "venv", "env")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retry_after(err: urllib.error.HTTPError) -> float:
    try:
        value = float(err.headers.get("Retry-After", 1))
    except (TypeError, ValueError, AttributeError):
        value = 1
    return max(0.0, min(value, MAX_RETRY_AFTER))


def _fetch_json(url: str, opener, log: list[dict], service: str):
    """Return (json|None, status) where status is ok|not_found|error."""
    log.append({"service": service, "url": url})
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with opener(req, timeout=TIMEOUT) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "not_found"
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                sleep(_retry_after(e))
                continue
            return None, "error"
        except Exception:
            return None, "error"
        try:
            return json.loads(body), "ok"
        except ValueError:
            return None, "error"
    return None, "error"


# ---- interpreting registry / metadata answers --------------------------------------------

def _from_candidates(expression: str | None, classifiers: list[str], free_text: str | None) -> dict:
    """Pick the best license string. Returns {spdx, source_field, confidence, note}."""
    if expression:
        spdx = licenses.normalize(expression)
        if spdx:
            return {"spdx": spdx, "field": "license expression", "confidence": "high", "note": None}
    ids = []
    for c in classifiers:
        if c.lower().startswith("license ::"):
            n = licenses.normalize(c)
            if n and n not in ids:
                ids.append(n)
    if free_text:
        text = free_text.strip()
        n = licenses.detect_from_text(text) if len(text) > 200 else licenses.normalize(text)
        if n:
            if ids and n not in ids:
                ids.append(n)
            elif not ids:
                return {"spdx": n, "field": "license field", "confidence": "medium", "note": None}
    if len(ids) == 1:
        return {"spdx": ids[0], "field": "classifier", "confidence": "medium", "note": None}
    if len(ids) > 1:
        return {"spdx": " OR ".join(ids), "field": "classifiers", "confidence": "low",
                "note": "multiple license classifiers; read as alternatives (OR), which may be wrong"}
    return {"spdx": None, "field": None, "confidence": None, "note": None}


def parse_pypi(doc: dict) -> dict:
    info = doc.get("info", {}) or {}
    return _from_candidates(info.get("license_expression"), info.get("classifiers") or [], info.get("license"))


def parse_npm(doc: dict) -> dict:
    lic = doc.get("license")
    if isinstance(lic, dict):
        lic = lic.get("type")
    if lic is None and isinstance(doc.get("licenses"), list):
        parts = [x.get("type") for x in doc["licenses"] if isinstance(x, dict) and x.get("type")]
        lic = " OR ".join(parts) if parts else None
    if isinstance(lic, str) and lic.upper().startswith("SEE LICENSE"):
        return {"spdx": None, "field": "license", "confidence": None, "note": "license is a pointer to a file"}
    spdx = licenses.normalize(lic) if isinstance(lic, str) else None
    return {"spdx": spdx, "field": "license" if spdx else None, "confidence": "high" if spdx else None, "note": None}


def parse_metadata_file(text: str) -> dict:
    """Parse a PEP 566 METADATA / PKG-INFO header block."""
    head = text.split("\n\n", 1)[0]
    expr = lic = None
    cls = []
    for line in head.splitlines():
        if line.startswith("License-Expression:"):
            expr = line.split(":", 1)[1].strip()
        elif line.startswith("License:") and lic is None:
            lic = line.split(":", 1)[1].strip()
        elif line.startswith("Classifier: License ::"):
            cls.append(line.split(":", 1)[1].strip())
    return _from_candidates(expr, cls, lic)


# ---- local metadata ----------------------------------------------------------------------

def _py_norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def local_python_index(root: Path) -> dict[tuple[str, str], Path]:
    index: dict[tuple[str, str], Path] = {}
    for venv in VENV_DIRS:
        base = root / venv
        if not base.is_dir():
            continue
        for sp in list(base.glob("Lib/site-packages")) + list(base.glob("lib/python*/site-packages")):
            for meta in sp.glob("*.dist-info"):
                m = re.match(r"^(.+?)-(\d[^-]*)\.dist-info$", meta.name)
                if m and (meta / "METADATA").is_file():
                    index[(_py_norm(m.group(1)), m.group(2))] = meta / "METADATA"
    return index


def local_npm(root: Path, name: str, version: str | None) -> dict | None:
    pj = root / "node_modules" / Path(*name.split("/")) / "package.json"
    if not pj.is_file():
        return None
    try:
        doc = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return None
    if version and doc.get("version") != version:
        return None
    return parse_npm(doc)


# ---- driver ------------------------------------------------------------------------------

def resolve_all(deps: list[dict], root: Path, offline: bool = False, include_dev: bool = False,
                opener=None) -> dict:
    """Attach license info to each dependency.

    Returns {dependencies: [... + license, license_evidence, license_confidence, lookup_status],
    requests_log: [...]}. lookup_status: lockfile|local|registry|not_looked_up|unregistered_source|
    not_found|lookup_failed|offline_no_local_data.
    """
    if opener is None:
        opener = urllib.request.urlopen
    log: list[dict] = []
    py_index = local_python_index(Path(root))
    out = []
    first_request = True
    for d in deps:
        d = dict(d)
        d.update({"license": None, "license_evidence": None, "license_confidence": None,
                  "license_note": None, "lookup_status": None})
        found = None
        if d["source"] != "registry":
            d["lookup_status"] = "unregistered_source"
            out.append(d)
            continue
        if d.get("license_hint"):
            n = licenses.normalize(d["license_hint"]) if isinstance(d["license_hint"], str) else None
            if n:
                found = {"spdx": n, "field": "lockfile license field", "confidence": "medium", "note": None}
                d["lookup_status"] = "lockfile"
        if not found and d["ecosystem"] == "pypi" and d["version"]:
            meta = py_index.get((_py_norm(d["name"]), d["version"]))
            if meta:
                f = parse_metadata_file(meta.read_text(encoding="utf-8", errors="replace"))
                if f["spdx"]:
                    found, d["lookup_status"] = f, "local"
        if not found and d["ecosystem"] == "npm":
            f = local_npm(Path(root), d["name"], d["version"])
            if f and f["spdx"]:
                found, d["lookup_status"] = f, "local"
        if not found:
            if offline:
                d["lookup_status"] = "offline_no_local_data"
            elif d["scope"] == "dev" and not include_dev:
                d["lookup_status"] = "not_looked_up"
            else:
                if not first_request:
                    sleep(REQUEST_DELAY)
                first_request = False
                found, status = _registry(d, opener, log)
                d["lookup_status"] = "registry" if found and found["spdx"] else status
        if found and found["spdx"]:
            d["license"] = found["spdx"]
            d["license_evidence"] = found["field"] if d["lookup_status"] != "registry" else f"{d['ecosystem']} registry: {found['field']}"
            d["license_confidence"] = found["confidence"]
            d["license_note"] = found["note"]
        elif found and found["note"]:
            d["license_note"] = found["note"]
        out.append(d)
    return {"dependencies": out, "requests_log": log}


def _registry(d: dict, opener, log: list[dict]):
    name, version = d["name"], d["version"]
    if d["ecosystem"] == "pypi":
        quoted = urllib.parse.quote(name, safe="")
        url = f"{PYPI}{quoted}/{urllib.parse.quote(version, safe='')}/json" if version else f"{PYPI}{quoted}/json"
        doc, status = _fetch_json(url, opener, log, "pypi.org")
        if doc is None:
            return None, ("not_found" if status == "not_found" else "lookup_failed")
        res = parse_pypi(doc)
        if not version:
            res["note"] = (res["note"] + "; " if res["note"] else "") + "version unpinned: license of the latest release"
        return res, "ok"
    quoted = urllib.parse.quote(name, safe="@").replace("/", "%2F")
    url = f"{NPM}{quoted}/{urllib.parse.quote(version, safe='')}" if version else f"{NPM}{quoted}/latest"
    doc, status = _fetch_json(url, opener, log, "registry.npmjs.org")
    if doc is None:
        return None, ("not_found" if status == "not_found" else "lookup_failed")
    res = parse_npm(doc)
    if not version:
        res["note"] = (res["note"] + "; " if res["note"] else "") + "version unpinned: license of the latest release"
    return res, "ok"
