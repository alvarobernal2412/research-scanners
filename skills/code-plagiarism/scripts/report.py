"""Report assembly: local analysis plus the backend results files, as one JSON report.

This script never opens a network connection. It reads the project (header and SPDX detection, references
in comments, licence files), reads the results files written by `deps.py`, `scanoss_backend.py` and
`stackexchange.py`, and merges them. Matches that overlap in one file become one finding with all their
candidate origins. Candidates are leads, never an established origin; the report makes no claim about who
copied from whom and gives no legal verdict. Zero findings is reported as "No matches found in the checked
scope", followed by what was and was not checked; a tier that failed or was skipped is always listed.

    python -I report.py <root> [--results R ...] [--no-scancode]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, OrderedDict
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from headers import (detect_header, effective_file_license, find_references, project_licenses,  # noqa: E402
                     run_scancode, scancode_status)
from inventory import InventoryError, inventory, read_lines  # noqa: E402
from model import load_results, raw_match_from_dict  # noqa: E402
from triage import Block, declared_tree_reason, trivial_reason  # noqa: E402

DISCLAIMER = ("Evidence of textual similarity and licence information only; not a legal opinion and not a "
              "finding about who copied from whom.")
DIRECTION_NOTE = ("Candidate origins show where similar code exists; similarity cannot establish who copied from "
                  "whom or which copy is the original.")
MAX_MATCHED_LINES = 60
MAX_LINE_CHARS = 300
MAX_REFERENCES = 500
MAX_DROPPED = 500
MAX_REQUESTS = 2000
SKIP_EXAMPLES = 5

TIER_ORDER = ("strict-local", "strict", "fingerprint", "open")
SERVICES = {"clearlydefined": "api.clearlydefined.io", "scanoss": "api.osskb.org",
            "stackexchange": "api.stackexchange.com"}
_TYPE_RANK = {"file": 0, "exact": 0, "normalised": 1, "near": 2}
_CONF_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}
_EVIDENCE_RANK = {"so_revision": 0, "scanoss_license_file": 1, "clearlydefined": 2, "so_answer_current": 3,
                  "scanoss_component_declared": 4, "so_date_rule": 5, "scanoss_other": 6}
_COPYLEFT = ("GPL", "AGPL", "LGPL", "CC-BY-SA", "MPL")
_OBLIGATIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "obligations.json"


class ReportError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def _obligations() -> dict:
    return json.loads(_OBLIGATIONS_PATH.read_text(encoding="utf-8"))


# ---- licence helpers ------------------------------------------------------------------------------

def _base(spdx) -> str:
    s = (spdx or "").strip().lower()
    for suffix in ("-or-later", "-only", "+"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    return s


def _is_copyleft(spdx) -> bool:
    return bool(spdx) and spdx.upper().startswith(_COPYLEFT)


def _conf_label(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return "high" if value >= 0.7 else "medium" if value >= 0.4 else "low"
    return value if value in ("high", "medium", "low") else None


def _norm_license(lic) -> dict | None:
    if not isinstance(lic, dict):
        return None
    out = dict(lic)
    out["confidence"] = _conf_label(lic.get("confidence"))
    out.setdefault("spdx", None)
    out.setdefault("status", "unknown")
    out.setdefault("evidence", None)
    return out


def _lic_key(lic: dict):
    return (lic.get("spdx"), lic.get("status"), lic.get("evidence"))


def _best_license(licenses: list) -> dict | None:
    if not licenses:
        return None
    return sorted(licenses, key=lambda l: (l.get("status") != "declared", _CONF_RANK.get(l.get("confidence"), 3),
                                           _EVIDENCE_RANK.get(l.get("evidence"), 9)))[0]


def _obligation(cand: dict, templates: dict, project_text: str) -> str:
    lic = _best_license(cand.get("licenses") or [])
    if cand.get("kind") == "stackoverflow":
        spdx = lic["spdx"] if lic and lic.get("spdx") else "its Stack Overflow licence (CC BY-SA; version not determined)"
        text = templates["stackoverflow"].format(
            license=spdx, author=cand.get("author") or "the answer's author",
            author_url=cand.get("author_url") or "profile link not available", origin_url=cand["origin_url"])
        others = sorted({l["spdx"] for l in cand.get("licenses", []) if l.get("spdx") and l is not lic
                         and l["spdx"] != spdx})
        if others:
            text += " Other licence evidence for this answer: " + ", ".join(others) + "; check the answer's timeline."
        return text
    if lic is None or lic.get("status") == "unknown" or (lic.get("status") == "declared" and not lic.get("spdx")):
        return templates["unknown"]
    if lic["status"] == "none_found":
        return templates["no_license"]
    if _is_copyleft(lic["spdx"]):
        return templates["copyleft"].format(license=lic["spdx"], project_license=project_text)
    return templates["permissive"].format(license=lic["spdx"])


# ---- loading results ---------------------------------------------------------------------------------

def _load_results(paths: list, root_name: str) -> list:
    loaded = []
    for p in paths:
        label = Path(p).name
        try:
            data = load_results(Path(p))
        except (OSError, ValueError, RecursionError) as exc:
            raise ReportError("results_invalid", f"{label}: {type(exc).__name__}") from None
        if data.get("tier") not in ("strict", "fingerprint", "open"):
            raise ReportError("results_invalid", f"{label}: unknown tier")
        if not isinstance(data.get("backend"), str) or not data["backend"]:
            raise ReportError("results_invalid", f"{label}: bad backend")
        for key in ("requests_sent", "not_sent", "matches", "dropped", "dependencies", "errors", "notes"):
            if not isinstance(data.get(key, []), list):
                raise ReportError("results_invalid", f"{label}: {key} must be a list")
        # a run that failed before it could read its plan records an empty root together with the error
        failed_early = data["root"] in ("", None) and bool(data.get("errors"))
        if not failed_early and data["root"] != root_name:
            raise ReportError("results_root_mismatch",
                              f"{label} was made for '{data['root']}', not '{root_name}'")
        matches = []
        for raw in data.get("matches", []):
            try:
                m = raw_match_from_dict(raw)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ReportError("results_invalid", f"{label}: a match is malformed") from None
            if (not isinstance(m.rel, str) or not 1 <= m.start <= m.end or m.match_type not in _TYPE_RANK
                    or not isinstance(m.candidate.origin_url, str) or not m.candidate.origin_url):
                raise ReportError("results_invalid", f"{label}: a match is malformed")
            matches.append(m)
        data["_matches"] = matches
        loaded.append(data)
    return loaded


# ---- files ----------------------------------------------------------------------------------------------

_COMMENT_MARK = re.compile(r"(?<!:)//|/\*|(?<![\w&])#|^\s*\*|--\s")
_CUE = re.compile(r"\b(?:copied|adapted|taken|derived|originally|based on|source|credit|credits|via|thanks|courtesy|"
                  r"inspired|attribution|author|licen[cs]e|see|refs?|reference)\b", re.I)


def _is_attribution_ref(ref, line: str) -> bool:
    """Whether a URL is plausibly a reference to a source rather than a URL the code uses. Stack Overflow and
    GitHub links always count. Other URLs count when the line holds a comment marker before the URL or words
    such as "copied from" / "source" / "see" (this also catches docstrings). A URL in a string that the code
    uses (an API endpoint) does not."""
    if ref.kind in ("stackoverflow", "github"):
        return True
    before = line[:max(line.find(ref.url), 0)]
    return bool(_COMMENT_MARK.search(before) or _CUE.search(line))


class _File:
    def __init__(self, sf, lines, header, refs):
        self.sf, self.lines, self.header, self.refs = sf, lines, header, refs
        self.scancode: list = []

    @property
    def evidence(self) -> list:
        return self.header + self.scancode


def _ref_matches(ref, cand: dict) -> bool:
    url = cand["origin_url"].lower().rstrip("/")
    if ref.kind == "stackoverflow":
        pid = cand.get("post_id")
        return pid is not None and ref.post_id == pid
    if ref.kind == "github":
        def repo(u):
            parts = u.lower().split("github.com/", 1)[-1].split("/")
            return "/".join(parts[:2]) if len(parts) >= 2 else None
        return "github.com/" in url and repo(ref.url) is not None and repo(ref.url) == repo(url)
    return ref.url.lower().rstrip("/") == url


def _attribution(file: _File, candidates: list) -> dict:
    ev = []
    for ref in file.refs:
        if any(_ref_matches(ref, c) for c in candidates):
            ev.append({"line": ref.line, "text": ref.url[:200], "kind": "reference"})
    cand_bases = {_base(l["spdx"]) for c in candidates for l in c.get("licenses", []) if l.get("spdx")}
    for e in file.evidence:
        if e.spdx and e.kind in ("spdx_tag", "license_text") and _base(e.spdx) in cand_bases:
            ev.append({"line": e.line, "text": e.text[:200], "kind": "header_license"})
    if ev:
        return {"status": "present", "evidence": ev}
    other = [{"line": r.line, "text": r.url[:200], "kind": "reference"} for r in file.refs]
    other += [{"line": e.line, "text": e.text[:200], "kind": "copyright"} for e in file.evidence
              if e.kind == "copyright"]
    if other:
        return {"status": "other_reference", "evidence": other[:10]}
    return {"status": "absent", "evidence": []}


def _merge_candidates(matches: list) -> list:
    by: OrderedDict = OrderedDict()
    for m in matches:
        c = asdict(m.candidate)
        c["licenses"] = [l for l in (_norm_license(x) for x in c.get("licenses") or []) if l]
        d = by.get(c["origin_url"])
        if d is None:
            c["backends"] = [c["backend"]]
            by[c["origin_url"]] = c
            continue
        if c["backend"] not in d["backends"]:
            d["backends"].append(c["backend"])
        seen = {_lic_key(l) for l in d["licenses"]}
        d["licenses"] += [l for l in c["licenses"] if _lic_key(l) not in seen]
        for k in ("author", "author_url", "date", "revision", "post_id", "oss_lines"):
            if d.get(k) is None and c.get(k) is not None:
                d[k] = c[k]
        pcts = [p for p in (d.get("matched_pct"), c.get("matched_pct")) if p is not None]
        d["matched_pct"] = max(pcts) if pcts else None
        d["purl"] = list(dict.fromkeys((d.get("purl") or []) + (c.get("purl") or [])))
        if c.get("note") and c["note"] not in (d.get("note") or ""):
            d["note"] = ((d.get("note") or "") + " " + c["note"]).strip()
    return sorted(by.values(), key=lambda c: (c.get("date") or "9999", c["origin_url"]))


def _clusters(matches: list) -> list:
    matches = sorted(matches, key=lambda m: (m.start, m.end))
    out: list = []
    for m in matches:
        if out and m.start <= out[-1]["end"]:
            out[-1]["matches"].append(m)
            out[-1]["end"] = max(out[-1]["end"], m.end)
        else:
            out.append({"start": m.start, "end": m.end, "matches": [m]})
    return out


# ---- report ----------------------------------------------------------------------------------------------

def _service(backend: str) -> str:
    return SERVICES.get(backend, backend)


def build_report(root: Path, results_paths: list, use_scancode: bool = True, scancode_runner=None) -> dict:
    root = Path(root)
    inv = inventory(root, require_sources=False)
    if not inv.sources:
        raise InventoryError("no_source_files", f"no source files under {root}")
    results = _load_results(list(results_paths), inv.root.name)
    templates = _obligations()

    # ---- local analysis
    files: dict = {}
    for sf in inv.sources:
        try:
            lines = read_lines(sf.path)
        except OSError:
            continue
        refs = [r for r in find_references(lines) if _is_attribution_ref(r, lines[r.line - 1])]
        files[sf.rel] = _File(sf, lines, detect_header(lines), refs)
    project = project_licenses(inv)
    notes: list = []
    scancode = "disabled"
    if use_scancode:
        if scancode_status() == "not_installed":
            scancode = "not_installed"
            notes.append("ScanCode is not installed: licence headers were detected with the built-in detector "
                         "(SPDX tags and common licence texts). Optional: pip install scancode-toolkit.")
        else:
            kw = {"runner": scancode_runner} if scancode_runner is not None else {"runner": subprocess.run}
            found = run_scancode(inv.root, list(files), **kw)
            if found is None:
                scancode = "failed"
                notes.append("ScanCode is installed but its run failed; the built-in detector was used.")
            else:
                scancode = "used"
                for rel, ev in found.items():
                    if rel in files:
                        files[rel].scancode = list(ev)

    # ---- results
    tiers = ["strict-local"] + [r["tier"] for r in results]
    tiers_run = [t for t in TIER_ORDER if t in tiers]
    dropped: list = []
    raw_matches: list = []
    dependencies: list = []
    requests: list = []
    not_sent: list = []
    errors: list = []
    services: list = []
    kb_version = None
    for r in results:
        b = r["backend"]
        for m in r["_matches"]:
            raw_matches.append(m)
        for d in r.get("dropped", []):
            if isinstance(d, dict):
                lines = d.get("lines") if d.get("lines") is not None else (
                    f"{d.get('start')}-{d.get('end')}" if d.get("start") is not None else None)
                dropped.append({"file": d.get("rel"), "lines": lines, "reason": d.get("reason"), "backend": b})
        for d in r.get("dependencies", []):
            if isinstance(d, dict):
                dep = dict(d)
                lic = _norm_license(d.get("license")) if isinstance(d.get("license"), dict) else None
                dep["license"] = lic
                dependencies.append(dep)
        for s in r.get("requests_sent", []):
            if isinstance(s, dict):
                requests.append({"backend": b, "method": s.get("method"), "url": s.get("url"),
                                 "status": s.get("status") if s.get("status") is not None else s.get("error")})
        if r.get("requests_sent") and _service(b) not in services:
            services.append(_service(b))
        for n in r.get("not_sent", []):
            if isinstance(n, dict):
                not_sent.append({"backend": b, "id": n.get("id"), "reason": n.get("reason")})
        for e in r.get("errors", []):
            if isinstance(e, dict):
                errors.append({"backend": b, "code": e.get("code"), "detail": e.get("detail")})
        kb_version = kb_version or r.get("kb_version")
        for n in r.get("notes", []):
            if isinstance(n, str):
                notes.append(f"{b}: {n}")

    # ---- findings
    by_file: dict = {}
    for m in raw_matches:
        f = files.get(m.rel)
        if f is None:
            dropped.append({"file": m.rel, "lines": f"{m.start}-{m.end}", "backend": m.candidate.backend,
                            "reason": declared_tree_reason(m.rel) or "not_eligible"})
            continue
        end = min(m.end, len(f.lines))
        start = min(m.start, end)
        if end < 1:
            continue
        if m.match_type != "file":
            reason = trivial_reason(Block(m.rel, start, end, f.sf.lang, f.lines[start - 1:end]))
            if reason:
                dropped.append({"file": m.rel, "lines": f"{start}-{end}", "reason": reason,
                                "backend": m.candidate.backend})
                continue
        by_file.setdefault(m.rel, []).append(type(m)(m.rel, start, end, m.match_type, m.candidate))

    project_ev = next((e for e in project if e.spdx), None)
    findings: list = []
    for rel in sorted(by_file):
        f = files[rel]
        eff = effective_file_license(f.evidence, project)
        for cl in _clusters(by_file[rel]):
            cands = _merge_candidates(cl["matches"])
            attribution = _attribution(f, cands)
            target = eff.spdx if eff else None
            reasons = []
            if attribution["status"] != "present":
                reasons.append("unattributed")
            if target and any(_is_copyleft(l.get("spdx")) and _base(l["spdx"]) != _base(target)
                              for c in cands for l in c["licenses"]):
                reasons.append("license_conflict")
            if cands and all(c["licenses"] and all(l.get("status") == "none_found" for l in c["licenses"])
                             for c in cands):
                reasons.append("no_license_all_rights_reserved")
            project_text = target or "licence (none found)"
            for c in cands:
                c["obligation"] = _obligation(c, templates, project_text)
            if len(cands) == 1:
                obligation = cands[0]["obligation"]
            else:
                obligation = "\n".join(f"{c['origin_url']}: {c['obligation']}" for c in cands)
            fnotes = []
            if len(cands) > 1 or any(c["kind"] == "repository" for c in cands):
                fnotes.append(DIRECTION_NOTE)
            for c in cands:
                if c.get("note"):
                    fnotes.append(c["note"])
            if eff is not None:
                fnotes.append(f"Effective licence of this file: {eff.spdx} ({eff.source}, {eff.confidence} confidence)"
                              + ("; the file's own header takes precedence over the project licence."
                                 if eff.source == "file_header" else "."))
            elif project_ev is None:
                fnotes.append("No licence was found for this file or the project: by default all rights are "
                              "reserved by the author.")
            if "license_conflict" in reasons:
                fnotes.append(f"The candidate's copyleft licence differs from this file's effective licence "
                              f"({target}); how that matters depends on how the code was obtained and used. "
                              "This is not a legal opinion.")
            start, end = cl["start"], cl["end"]
            shown = f.lines[start - 1:end]
            if len(shown) > MAX_MATCHED_LINES:
                shown = shown[:MAX_MATCHED_LINES]
                fnotes.append(f"matched lines truncated to the first {MAX_MATCHED_LINES} of {end - start + 1}.")
            findings.append({
                "id": "", "file": rel, "lines": [start, end],
                "matched_lines": [ln[:MAX_LINE_CHARS] for ln in shown],
                "match_type": min((m.match_type for m in cl["matches"]), key=lambda t: _TYPE_RANK[t]),
                "candidates": cands,
                "local_license_evidence": [asdict(e) for e in f.evidence],
                "attribution": attribution, "flag": bool(reasons), "flag_reasons": reasons,
                "obligation": obligation, "notes": list(dict.fromkeys(fnotes)),
            })
    for n, fd in enumerate(findings, 1):
        fd["id"] = f"F{n}"

    references = [{"file": rel, "line": r.line, "url": r.url, "kind": r.kind}
                  for rel, f in files.items() for r in f.refs][:MAX_REFERENCES]

    skipped: dict = {}
    for s in inv.skipped:
        d = skipped.setdefault(s.reason, {"count": 0, "examples": []})
        d["count"] += 1
        if len(d["examples"]) < SKIP_EXAMPLES:
            d["examples"].append(s.rel)

    # ---- coverage
    ran = set(tiers_run)
    checked = ["licence headers, SPDX tags and licence files in your source files (local)",
               "references to Stack Overflow, GitHub and other URLs in comments (local)"]
    not_checked = []
    if "strict" in ran:
        checked.append("licences of declared direct dependencies with an exact version, via ClearlyDefined "
                       "(public package coordinates)")
    else:
        not_checked.append("licences of declared dependencies (strict tier)")
    if "fingerprint" in ran:
        checked.append("whole files and snippets of the files you chose, looked up by hash in the SCANOSS "
                       "knowledge base (fingerprint tier)")
    else:
        not_checked.append("snippets copied from public repositories and packages (fingerprint tier)")
    if "open" in ran:
        checked.append("code blocks of the files you chose, searched on Stack Overflow by rare identifiers and "
                       "compared locally (open tier)")
    else:
        not_checked.append("Stack Overflow answers by keyword search (open tier)")
    if ran & {"fingerprint", "open"}:
        not_checked.append("files outside the --include paths you chose")
    not_checked += ["unpublished or private sources",
                    "renamed or restructured copies beyond token normalisation"]
    if "fingerprint" in ran:
        not_checked.append("SCANOSS knowledge base snapshot dates (Stack Overflow data ~2022)")
    for t in ("strict", "fingerprint", "open"):
        problems = [e for e in errors if _tier_of(results, e["backend"]) == t]
        unsent = [n for n in not_sent if _tier_of(results, n["backend"]) == t]
        if problems or unsent:
            not_checked.append(f"{t} tier incomplete: {len(problems)} error(s), {len(unsent)} request(s) not sent "
                               "(see errors and not_sent); the part of the scope they cover was NOT checked")
    if tiers_run == ["strict-local"]:
        notes.append("Only the local checks ran. For similarity matches, ask for the fingerprint tier (SCANOSS) "
                     "and the open tier (Stack Overflow); both send data derived from your files and run only "
                     "after you approve the exact request.")
    if len(references) >= MAX_REFERENCES:
        notes.append(f"references_in_comments truncated to {MAX_REFERENCES}.")
    if len(dropped) > MAX_DROPPED:
        notes.append(f"dropped list truncated to {MAX_DROPPED} of {len(dropped)}.")
    if len(requests) > MAX_REQUESTS:
        notes.append(f"requests list truncated to {MAX_REQUESTS} of {len(requests)}.")

    flagged = sum(1 for fd in findings if fd["flag"])
    if findings:
        head = (f"{len(findings)} finding(s) in {len({fd['file'] for fd in findings})} file(s); {flagged} flagged "
                "for review. Candidate origins are leads: similarity cannot establish who copied from whom.")
    else:
        head = "No matches found in the checked scope."
    parts = [head]
    if errors or not_sent:
        parts.append(f"Part of the scope was NOT checked: {len(errors)} lookup error(s) "
                     f"({', '.join(sorted({str(e['code']) for e in errors})) or 'requests not sent'}) and "
                     f"{len(not_sent)} request(s) not sent.")
    parts.append("Checked: " + "; ".join(checked) + ".")
    parts.append("Not checked: " + "; ".join(not_checked) + ".")
    coverage = {
        "tiers_run": tiers_run, "services_contacted": services, "requests": requests[:MAX_REQUESTS],
        "not_sent": not_sent, "errors": errors, "skipped_files": skipped, "checked": checked,
        "not_checked": not_checked, "scancode": scancode, "kb_version": kb_version,
        "eligible_files": len(files), "notes": notes,
    }
    return {
        "root": inv.root.name, "tiers_run": tiers_run, "project_license": [asdict(e) for e in project],
        "findings": findings, "references_in_comments": references, "dependencies": dependencies,
        "dropped": dropped[:MAX_DROPPED], "coverage": coverage, "summary": " ".join(parts),
        "disclaimer": DISCLAIMER,
    }


def _tier_of(results: list, backend: str):
    for r in results:
        if r["backend"] == backend:
            return r["tier"]
    return None


# ---- CLI ---------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": "usage", "detail": message}))
        raise SystemExit(2)


def main(argv: list) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = _Parser(prog="report.py")
    ap.add_argument("root")
    ap.add_argument("--results", nargs="+", action="extend", default=[])
    ap.add_argument("--no-scancode", action="store_true")
    args = ap.parse_args(argv)
    try:
        rep = build_report(Path(args.root), [Path(p) for p in args.results], use_scancode=not args.no_scancode)
        json.dumps(rep, allow_nan=False)
    except (ReportError, InventoryError) as exc:
        print(json.dumps({"error": exc.code, "detail": getattr(exc, "detail", None) or str(exc)}))
        return 2
    except (OSError, ValueError):
        print(json.dumps({"error": "output_error", "detail": "file system or encoding error"}))
        return 2
    print(json.dumps(rep, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
