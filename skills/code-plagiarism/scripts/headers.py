"""Licence evidence: per-file headers, SPDX tags, repo licences, attribution references.

Reports evidence only; it never decides what licence applies in law.
"""
from __future__ import annotations

import bisect
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inventory import Inventory, read_lines  # noqa: E402
from model import find_exe  # noqa: E402

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]

HEADER_SCAN_LINES = 60
MAX_HEADER_LINE_CHARS = 1000
MAX_SPDX_ID_CHARS = 160
_SIGNATURES_PATH = Path(__file__).resolve().parent.parent / "data" / "license_signatures.json"

_COMMENT_PREFIX = re.compile(r"^\s*(?:/\*+|//+|#+|<!--|--|;+|%+|\*+)?\s*")
_COMMENT_SUFFIX = re.compile(r"\s*(?:\*+/|-->)\s*$")
_SPDX_TAG = re.compile(r"SPDX-License-Identifier:[ \t]*(\S.*?)[ \t]*(?:\*+/|-->|$)")
_COPYRIGHT = re.compile(r"copyright\b|©|\(c\)\s*\d{4}", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s)\]>\"'<]+")
_SPDX_SPLIT = re.compile(r"\s+(?:OR|AND)\s+", re.IGNORECASE)
_ALIASES = {
    "gpl-2.0": "GPL-2.0-only", "gpl-2.0+": "GPL-2.0-or-later",
    "gpl-3.0": "GPL-3.0-only", "gpl-3.0+": "GPL-3.0-or-later",
    "lgpl-2.1-only": "LGPL-2.1", "lgpl-3.0-only": "LGPL-3.0",
    "agpl-3.0-only": "AGPL-3.0",
}
_SO_HOSTS = ("stackoverflow.com", "stackexchange.com", "superuser.com",
             "serverfault.com", "askubuntu.com", "mathoverflow.net")


@dataclass
class LicenseEvidence:
    spdx: str | None
    kind: str          # spdx_tag | license_text | copyright
    line: int
    text: str
    source: str        # file_header | repo_license | package_manifest | scancode
    confidence: str    # high | medium | low


@dataclass
class Reference:
    kind: str          # stackoverflow | github | url
    url: str
    line: int
    post_id: int | None


_SIGS: dict[str, dict[str, list[re.Pattern]]] | None = None


def _sigs() -> dict[str, dict[str, list[re.Pattern]]]:
    """{spdx: {"header": [...], "mention": [...]}}; header = full-notice phrases."""
    global _SIGS
    if _SIGS is None:
        raw = json.loads(_SIGNATURES_PATH.read_text(encoding="utf-8"))
        _SIGS = {sid: {strength: [re.compile(p, re.IGNORECASE | re.DOTALL) for p in pats]
                       for strength, pats in groups.items()}
                 for sid, groups in raw.items()}
    return _SIGS


def canonical_spdx(raw: str) -> tuple[str, bool]:
    """(id in canonical case, known?)."""
    key = raw.strip()
    low = key.lower()
    if low in _ALIASES:
        return _ALIASES[low], True
    for sid in _sigs():
        if sid.lower() == low:
            return sid, True
    return key, False


def _normalise(lines: list[str]) -> tuple[str, list[int]]:
    """Join comment-stripped lines with single spaces; return text and line start offsets."""
    parts: list[str] = []
    starts: list[int] = []
    pos = 0
    for ln in lines:
        s = _COMMENT_SUFFIX.sub("", _COMMENT_PREFIX.sub("", ln, count=1))
        s = re.sub(r"\s+", " ", s).strip()
        starts.append(pos)
        parts.append(s)
        pos += len(s) + 1
    return " ".join(parts), starts


def _match_signatures(lines: list[str]) -> list[tuple[str, int, str, str]]:
    """(spdx, line, snippet, strength); strength is "header" or "mention"."""
    text, starts = _normalise(lines)
    found: list[tuple[str, int, str, str]] = []
    for sid, groups in _sigs().items():
        for strength in ("header", "mention"):
            hit = None
            for pat in groups.get(strength, []):
                hit = pat.search(text)
                if hit:
                    break
            if not hit:
                continue
            idx, snippet = hit.start(), hit.group(0)[:160]
            if sid == "BSD-2-Clause":  # pattern is anchored at text start
                idx = max(text.lower().find("redistribution and use"), 0)
                snippet = text[idx: idx + 160]
            line = bisect.bisect_right(starts, idx)
            found.append((sid, max(line, 1), snippet, strength))
            break
    return found


def _split_spdx(expr: str) -> list[str]:
    expr = expr.strip().strip("()").strip()
    return [p.strip().strip("()").strip() for p in _SPDX_SPLIT.split(expr)
            if p.strip().strip("()")]


def detect_header(lines: list[str]) -> list[LicenseEvidence]:
    head = [ln[:MAX_HEADER_LINE_CHARS] for ln in lines[:HEADER_SCAN_LINES]]
    out: list[LicenseEvidence] = []
    for i, ln in enumerate(head, 1):
        m = _SPDX_TAG.search(ln)
        if m:
            for part in _split_spdx(m.group(1)):
                sid, known = canonical_spdx(part[:MAX_SPDX_ID_CHARS])
                out.append(LicenseEvidence(sid, "spdx_tag", i, ln.strip(), "file_header",
                                           "high" if known else "medium"))
    for sid, line, snippet, strength in _match_signatures(head):
        out.append(LicenseEvidence(sid, "license_text", line, snippet, "file_header",
                                   "high" if strength == "header" else "medium"))
    for i, ln in enumerate(head, 1):
        if _COPYRIGHT.search(ln) and not _SPDX_TAG.search(ln):
            out.append(LicenseEvidence(None, "copyright", i, ln.strip(), "file_header", "low"))
    out.sort(key=lambda e: (e.line, e.kind))
    return out


def detect_license_file(text: str) -> LicenseEvidence | None:
    found = _match_signatures(re.split(r"\r\n|\n|\r", text))
    if not found:
        return None
    found.sort(key=lambda f: f[3] != "header")  # stable: full notices first
    sid, line, snippet, _ = found[0]
    return LicenseEvidence(sid, "license_text", line, snippet, "repo_license", "high")


def _so_post_id(url: str) -> int | None:
    for pat in (r"/a/(\d+)", r"/revisions/(\d+)", r"/questions/\d+/[^/#?]+/(\d+)",
                r"#(\d+)$", r"/q/(\d+)", r"/questions/(\d+)"):
        m = re.search(pat, url)
        if m:
            return int(m.group(1))
    return None


def find_references(lines: list[str]) -> list[Reference]:
    refs: list[Reference] = []
    for i, ln in enumerate(lines, 1):
        for m in _URL.finditer(ln):
            url = m.group(0).rstrip(".,;:!?")
            host = re.sub(r"^https?://", "", url).split("/")[0].lower().split(":")[0]
            if any(host == h or host.endswith("." + h) for h in _SO_HOSTS):
                refs.append(Reference("stackoverflow", url, i, _so_post_id(url)))
            elif host == "github.com" or host.endswith(".github.com"):
                refs.append(Reference("github", url, i, None))
            else:
                refs.append(Reference("url", url, i, None))
    return refs


def _manifest_license(path: Path, kind: str) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    if kind == "package.json":
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        lic = data.get("license") if isinstance(data, dict) else None
        if isinstance(lic, dict):
            lic = lic.get("type")
        return lic if isinstance(lic, str) and lic.strip() else None
    if kind == "pyproject":
        lic = None
        if tomllib is not None:
            try:
                lic = tomllib.loads(raw).get("project", {}).get("license")
                if isinstance(lic, dict):
                    lic = lic.get("text")
            except Exception:
                lic = None
        else:
            m = re.search(r'^\s*license\s*=\s*"([^"]+)"', raw, re.MULTILINE)
            lic = m.group(1) if m else None
        return lic if isinstance(lic, str) and lic.strip() else None
    return None


def project_licenses(inv: Inventory) -> list[LicenseEvidence]:
    out: list[LicenseEvidence] = []
    for rel in inv.license_files:
        try:
            text = "\n".join(read_lines(inv.root / rel))
        except OSError:
            continue
        ev = detect_license_file(text)
        if ev:
            ev.text = f"{rel}: {ev.text}"
            out.append(ev)
    for man in inv.manifests:
        if man.kind not in ("package.json", "pyproject"):
            continue
        lic = _manifest_license(inv.root / man.rel, man.kind)
        if not lic:
            continue
        for part in _split_spdx(lic):
            sid, known = canonical_spdx(part)
            out.append(LicenseEvidence(sid, "spdx_tag", 1, f"{man.rel}: license = {lic}",
                                       "package_manifest", "high" if known else "medium"))
    return out


def effective_file_license(file_ev: list[LicenseEvidence],
                           project: list[LicenseEvidence]) -> LicenseEvidence | None:
    """High-confidence per-file evidence beats a repo-level licence.

    Medium evidence (bare mentions, unknown ids) never displaces a project
    licence; it is used only when the project has none.
    """
    ranked = [ev for kind in ("spdx_tag", "license_text")
              for ev in file_ev if ev.spdx and ev.kind == kind]
    for ev in ranked:
        if ev.confidence == "high":
            return ev
    for ev in project:
        if ev.spdx:
            return ev
    for ev in ranked:
        if ev.confidence == "medium":
            return ev
    return None


def scancode_status(root=None) -> str:
    return "available" if find_exe("scancode", root) else "not_installed"


def _scancode_evidence(entry: dict) -> list[LicenseEvidence]:
    out: list[LicenseEvidence] = []
    seen: set[tuple] = set()
    for det in entry.get("license_detections") or []:
        for m in det.get("matches") or []:
            expr = m.get("license_expression_spdx") or m.get("license_expression") or ""
            score = float(m.get("score") or 0)
            conf = "high" if score >= 90 else "medium" if score >= 60 else "low"
            kind = "spdx_tag" if "spdx-id" in str(m.get("matcher", "")) else "license_text"
            for part in _split_spdx(expr):
                sid, _ = canonical_spdx(part)
                key = (sid, m.get("start_line"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(LicenseEvidence(sid, kind, int(m.get("start_line") or 1),
                                           str(m.get("matched_text") or expr)[:160],
                                           "scancode", conf))
    for c in entry.get("copyrights") or []:
        out.append(LicenseEvidence(None, "copyright", int(c.get("start_line") or 1),
                                   str(c.get("copyright") or "")[:160], "scancode", "medium"))
    return out


def run_scancode(root: Path, rels: list[str], runner=subprocess.run
                 ) -> dict[str, list[LicenseEvidence]] | None:
    exe = find_exe("scancode", root)
    if not exe:
        return None
    wanted = set(rels)
    with tempfile.TemporaryDirectory() as tmp:
        out_json = Path(tmp) / "scancode.json"
        cmd = [exe, "-l", "-c", "--json", str(out_json), "-n", "2", str(root)]
        try:
            res = runner(cmd, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            return None
        if getattr(res, "returncode", 0) != 0 or not out_json.exists():
            return None
        try:
            data = json.loads(out_json.read_text(encoding="utf-8"))
        except ValueError:
            return None
    result: dict[str, list[LicenseEvidence]] = {}
    for entry in data.get("files", []):
        if entry.get("type") != "file":
            continue
        path = str(entry.get("path", "")).replace("\\", "/")
        rel = path.split("/", 1)[1] if "/" in path else path
        if rel in wanted:
            result[rel] = _scancode_evidence(entry)
    return result
