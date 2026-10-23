"""SPDX normalisation and detection of a project's own license. Local only, no network."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

KNOWN = [
    "MIT", "Apache-2.0", "Apache-1.1", "BSD-2-Clause", "BSD-3-Clause", "BSD-4-Clause", "ISC", "0BSD",
    "Zlib", "Unlicense", "CC0-1.0", "PSF-2.0", "Python-2.0", "MPL-2.0", "MPL-1.1",
    "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later",
    "AGPL-3.0-only", "AGPL-3.0-or-later", "EPL-1.0", "EPL-2.0", "CDDL-1.0", "BSL-1.0",
    "Artistic-2.0", "WTFPL", "CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-NC-4.0", "BlueOak-1.0.0",
    "Unicode-DFS-2016", "HPND", "OFL-1.1", "SSPL-1.0", "BUSL-1.1", "Classpath-exception-2.0",
    "LicenseRef-Proprietary",
]
CANON = {k.lower(): k for k in KNOWN}
# deprecated SPDX ids and the "+" suffix
for _fam in ("GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0"):
    CANON[_fam.lower()] = _fam + "-only"
    CANON[_fam.lower() + "+"] = _fam + "-or-later"
    CANON[_fam.lower() + "-only"] = _fam + "-only"
    CANON[_fam.lower() + "-or-later"] = _fam + "-or-later"
CANON["lgpl-2.0"] = "LGPL-2.1-only"  # nearest known id; flagged by callers if it matters

ALIASES = {
    "mit license": "MIT", "the mit license": "MIT", "expat": "MIT", "mit/x11": "MIT",
    "apache": "Apache-2.0", "apache 2": "Apache-2.0", "apache 2.0": "Apache-2.0", "apache-2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0", "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0", "apache license": "Apache-2.0",
    "bsd": "BSD-3-Clause", "bsd license": "BSD-3-Clause", "new bsd": "BSD-3-Clause",
    "simplified bsd": "BSD-2-Clause", "bsd-3": "BSD-3-Clause", "bsd 3-clause": "BSD-3-Clause",
    "bsd-2": "BSD-2-Clause", "bsd 2-clause": "BSD-2-Clause",
    "isc license": "ISC", "python software foundation license": "PSF-2.0", "psf": "PSF-2.0",
    "mozilla public license 2.0": "MPL-2.0", "mpl 2.0": "MPL-2.0", "mpl": "MPL-2.0",
    "gplv2": "GPL-2.0-only", "gplv3": "GPL-3.0-only", "gplv2+": "GPL-2.0-or-later",
    "gplv3+": "GPL-3.0-or-later", "lgplv2": "LGPL-2.1-only",
    "lgplv2+": "LGPL-2.1-or-later", "lgplv3": "LGPL-3.0-only", "lgplv3+": "LGPL-3.0-or-later",
    "agplv3": "AGPL-3.0-only", "agplv3+": "AGPL-3.0-or-later",
    "public domain": "Unlicense", "the unlicense": "Unlicense", "cc0": "CC0-1.0",
    "proprietary": "LicenseRef-Proprietary", "commercial": "LicenseRef-Proprietary",
    "all rights reserved": "LicenseRef-Proprietary",
}
PLACEHOLDERS = {"", "unknown", "none", "n/a", "na", "see license", "see license file", "unlicensed",
                "other", "other/proprietary", "unspecified", "undefined", "null"}

_CLASSIFIER_PREFIX = "license :: "


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().strip("'\"")).lower()


def _free_text(low: str) -> str | None:
    """Heuristics for long free-text names. Returns an SPDX id or None."""
    if low.startswith(_CLASSIFIER_PREFIX):
        low = low[len(_CLASSIFIER_PREFIX):]
        low = re.sub(r"^(osi approved|other/proprietary license|freely distributable) :: ", "", low)
    if low in ALIASES:
        return ALIASES[low]
    if low in CANON:
        return CANON[low]
    later = bool(re.search(r"or later|or any later|\+|or-later", low))

    def gnu(fam_re: str, name: str, versions: dict[str, str]) -> str | None:
        if not re.search(fam_re, low):
            return None
        for needle, ver in versions.items():
            if re.search(needle, low):
                return f"{name}-{ver}-{'or-later' if later else 'only'}"
        return None

    for fam_re, name, versions in (
        (r"affero", "AGPL", {r"v?3": "3.0"}),
        (r"lesser|library general|\blgpl", "LGPL", {r"2\.1": "2.1", r"v?3": "3.0", r"v?2": "2.1"}),
        (r"general public license|\bgpl", "GPL", {r"v?3": "3.0", r"v?2": "2.0"}),
    ):
        got = gnu(fam_re, name, versions)
        if got:
            return got
    if re.search(r"apache", low):
        return "Apache-2.0" if re.search(r"2", low) or low in ("apache", "apache license") else "Apache-1.1"
    if "mozilla" in low:
        return "MPL-2.0" if "2" in low else "MPL-1.1"
    if re.search(r"\bmit\b", low):
        return "MIT"
    if "bsd" in low:
        if re.search(r"\b2\b|simplified|two", low):
            return "BSD-2-Clause"
        return "BSD-3-Clause"
    if "creative commons zero" in low or "cc0" in low:
        return "CC0-1.0"
    if "eclipse" in low:
        return "EPL-2.0" if "2" in low else "EPL-1.0"
    if "python software foundation" in low:
        return "PSF-2.0"
    return None


# ---- expressions -------------------------------------------------------------------------

_TOK = re.compile(r"\(|\)|[^\s()]+")


def _atom(tok: str) -> str | None:
    low = _clean(tok)
    return CANON.get(low) or ALIASES.get(low)


def parse_expression(expr: str):
    """Parse an SPDX-style expression into ('id', id, exception) / ('and'|'or', [nodes]).

    Returns None when any atom is not recognised.
    """
    toks = _TOK.findall(expr)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take():
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def p_or():
        nodes = [p_and()]
        while peek() and peek().upper() == "OR":
            take()
            nodes.append(p_and())
        return nodes[0] if len(nodes) == 1 else ("or", nodes)

    def p_and():
        nodes = [p_with()]
        while peek() and peek().upper() == "AND":
            take()
            nodes.append(p_with())
        return nodes[0] if len(nodes) == 1 else ("and", nodes)

    def p_with():
        if peek() == "(":
            take()
            node = p_or()
            if peek() != ")":
                raise ValueError("unbalanced")
            take()
            return node
        tok = take()
        if tok.upper() in ("AND", "OR", "WITH", ")"):
            raise ValueError("unexpected token")
        ident = _atom(tok)
        if ident is None:
            raise ValueError("unknown id")
        exc = None
        if peek() and peek().upper() == "WITH":
            take()
            exc = take()
        return ("id", ident, exc)

    try:
        node = p_or()
    except (ValueError, IndexError):
        return None
    return node if pos == len(toks) else None


def to_string(node) -> str:
    kind = node[0]
    if kind == "id":
        return node[1] + (f" WITH {node[2]}" if node[2] else "")
    inner = (" OR " if kind == "or" else " AND ").join(
        f"({to_string(n)})" if n[0] != "id" and n[0] != kind else to_string(n) for n in node[1])
    return inner


def normalize(text: str | None) -> str | None:
    """Return a canonical SPDX expression for a free-form license string, or None if unusable.

    None means: empty, a placeholder like 'UNKNOWN', or not recognised.
    """
    if text is None:
        return None
    low = _clean(text)
    if low in PLACEHOLDERS:
        return None
    node = parse_expression(text.strip().replace("/", " OR ") if re.fullmatch(r"[\w.\-+]+(/[\w.\-+]+)+", text.strip()) else text.strip())
    if node is not None:
        return to_string(node)
    got = _free_text(low)
    return got


# ---- project license detection -----------------------------------------------------------

_FINGERPRINTS = [
    ("AGPL-3.0-only", r"gnu affero general public license"),
    ("LGPL-3.0-only", r"gnu lesser general public license.{0,200}version 3"),
    ("LGPL-2.1-only", r"gnu lesser general public license.{0,200}version 2\.1|gnu library general public license"),
    ("GPL-3.0-only", r"gnu general public license.{0,200}version 3"),
    ("GPL-2.0-only", r"gnu general public license.{0,200}version 2"),
    ("MPL-2.0", r"mozilla public license(,)? (version|v\.?) ?2\.0"),
    ("Apache-2.0", r"apache license.{0,40}version 2\.0"),
    ("Unlicense", r"this is free and unencumbered software released into the public domain"),
    ("CC0-1.0", r"cc0 1\.0 universal"),
    ("MIT", r"permission is hereby granted, free of charge, to any person obtaining a copy"),
    ("ISC", r"permission to use, copy, modify, and/or distribute this software for any purpose with or without fee"),
    ("BSD-3-Clause", r"redistribution and use in source and binary forms.{0,1500}neither the name"),
    ("BSD-2-Clause", r"redistribution and use in source and binary forms"),
    ("Zlib", r"this software is provided 'as-is', without any express or implied warranty.{0,300}altered source versions"),
]
LICENSE_NAMES = re.compile(r"^(licen[sc]e|copying|unlicense|notice)([._-].*)?$", re.I)
SPDX_TAG = re.compile(r"SPDX-License-Identifier:\s*([^\r\n*]+)")


def detect_from_text(text: str) -> str | None:
    flat = re.sub(r"\s+", " ", text).lower()
    for spdx, rx in _FINGERPRINTS:
        if re.search(rx, flat):
            return spdx
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _from_pyproject(path: Path) -> list[tuple[str, str]]:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        return []
    try:
        data = tomllib.loads(_read(path))
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    proj = data.get("project", {})
    lic = proj.get("license")
    if isinstance(lic, str):
        out.append((lic, "pyproject.toml project.license"))
    elif isinstance(lic, dict):
        if "text" in lic:
            out.append((lic["text"], "pyproject.toml project.license.text"))
        elif "file" in lic:
            out.append((_read(path.parent / lic["file"]), f"pyproject.toml license file {lic['file']}"))
    for c in proj.get("classifiers", []) or []:
        if c.lower().startswith("license ::"):
            out.append((c, "pyproject.toml classifier"))
    poetry = data.get("tool", {}).get("poetry", {})
    if isinstance(poetry.get("license"), str):
        out.append((poetry["license"], "pyproject.toml tool.poetry.license"))
    return out


def detect_project_license(root: Path) -> dict:
    """Find the project's own license. Returns spdx (or None), evidence list and notes."""
    root = Path(root)
    evidence: list[dict] = []
    notes: list[str] = []
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if child.is_file() and LICENSE_NAMES.match(child.stem if child.suffix in (".md", ".txt", ".rst") else child.name):
            text = _read(child)
            spdx = detect_from_text(text)
            evidence.append({"source": child.name, "spdx": spdx, "kind": "license_file"})
            if spdx is None:
                notes.append(f"{child.name} was found but its text matched no known license")
    pp = root / "pyproject.toml"
    if pp.is_file():
        for raw, src in _from_pyproject(pp):
            spdx = detect_from_text(raw) if len(raw) > 200 else normalize(raw)
            evidence.append({"source": src, "spdx": spdx, "kind": "manifest"})
    pj = root / "package.json"
    if pj.is_file():
        try:
            data = json.loads(_read(pj))
        except json.JSONDecodeError:
            data = {}
        lic = data.get("license")
        if isinstance(lic, dict):
            lic = lic.get("type")
        if lic is None and isinstance(data.get("licenses"), list) and data["licenses"]:
            lic = " OR ".join(str(x.get("type", "")) for x in data["licenses"] if isinstance(x, dict))
        if lic:
            evidence.append({"source": "package.json license", "spdx": normalize(str(lic)), "kind": "manifest"})
    found = [e["spdx"] for e in evidence if e["spdx"]]
    distinct = sorted(set(found))
    spdx = None
    if len(distinct) == 1:
        spdx = distinct[0]
    elif len(distinct) > 1:
        # prefer the license file; report the disagreement
        file_ids = [e["spdx"] for e in evidence if e["kind"] == "license_file" and e["spdx"]]
        spdx = file_ids[0] if file_ids else distinct[0]
        notes.append("license sources disagree: " + ", ".join(distinct))
    if re.search(r"-only$", spdx or "") and spdx.startswith(("GPL", "LGPL", "AGPL")):
        notes.append("the license text alone cannot show 'or later'; check file headers for 'or later' wording")
    return {"spdx": spdx, "evidence": evidence, "notes": notes}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(detect_project_license(Path(sys.argv[1] if len(sys.argv) > 1 else ".")), indent=2))
