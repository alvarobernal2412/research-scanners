"""Check that every reference of a paper exists and matches the real record.

Usage: python -I check_refs.py <paper or .bib> [--offline] [--limit N]

Prints one JSON report. Report-only: it never edits the paper or the bibliography and gives no
verdict on who or what wrote the paper. 'not_found' means "not found in the indexes queried".
What leaves the machine: DOIs, arXiv ids, titles, first authors and years of the cited works
(never the paper's text), sent to Crossref, DataCite, OpenAlex and doi.org. --offline sends nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import refs_compare as rc  # noqa: E402
import refs_extract as rx  # noqa: E402
import refs_lookup as rl  # noqa: E402

DEFAULT_LIMIT = 500
DISCLAIMER = ("This checks that cited works exist and match the index records. 'not_found' means not found in "
              "Crossref, DataCite and OpenAlex, not that the work is fabricated. It does not check that a cited work "
              "supports the claim it is cited for, and it says nothing about who or what wrote the paper.")
ATTENTION = ("mismatch", "doi_not_registered", "not_found", "lookup_failed")


def _key(ref: dict) -> str:
    if ref.get("doi"):
        return "doi:" + ref["doi"].lower()
    if ref.get("arxiv"):
        return "arxiv:" + ref["arxiv"]
    if ref.get("title"):
        return f"title:{rc.norm(ref['title'])}|{ref.get('year')}"
    return "raw:" + rc.norm(ref.get("raw"))[:200]


def _public_record(rec: dict | None) -> dict | None:
    if not rec:
        return None
    return {k: rec.get(k) for k in ("doi", "title", "authors", "year", "venues", "type", "url", "source")}


def evaluate(ref: dict, session: rl.Session | None) -> dict:
    out = {"id": ref["id"], "raw": ref["raw"][:300], "parsed": {k: ref.get(k) for k in
           ("authors", "title", "year", "venue", "doi", "arxiv", "url")},
           "verdict": None, "differences": [], "record": None, "via": None, "note": None}
    if session is None:
        out.update(verdict="unchecked", note="offline mode: no lookups were made")
        return out
    has_id = bool(ref.get("doi") or ref.get("arxiv"))
    if not has_id and ref["kind"] == "web":
        out.update(verdict="unverifiable", note="web resource; not covered by the bibliographic indexes")
        return out
    if not has_id and not ref.get("title") and len(ref.get("raw", "")) < 25:
        out.update(verdict="unverifiable", note="too little information to search for")
        return out
    res = rl.resolve(session, ref)
    out["via"] = res["via"]
    st = res["status"]
    if st == "found":
        comp = rc.compare(ref, res["record"])
        out.update(verdict=comp["verdict"], differences=comp["differences"], record=_public_record(res["record"]))
        if not comp["comparable_fields"]:
            out["note"] = "only the identifier could be checked: the reference carries no other fields to compare"
        elif res["via"] == "title_search":
            out["note"] = "matched by title search; the reference gave no identifier"
    elif st == "doi_not_registered":
        out.update(verdict="doi_not_registered", note="the DOI handle service does not know this DOI")
        if res.get("alternative_record"):
            out["alternative_record"] = _public_record(res["alternative_record"])
            out["note"] += "; a record with the same title exists under a different DOI"
    elif st == "doi_registered_unindexed":
        out.update(verdict="unverifiable", note="the DOI is registered but not in Crossref or DataCite, so its "
                   "metadata could not be compared")
    elif st == "not_found":
        out.update(verdict="not_found", note="no accepted match in the indexes queried")
    else:
        out.update(verdict="lookup_failed", note="a service did not answer; try again later")
    return out


def build_report(path: Path, offline: bool = False, limit: int = DEFAULT_LIMIT, opener=None) -> dict:
    extracted = rx.extract_references(path)
    refs = extracted["references"]
    coverage = list(extracted["notes"])
    if len(refs) > limit:
        coverage.append(f"Only the first {limit} of {len(refs)} references were checked (--limit).")
        refs = refs[:limit]
    session = None if offline else rl.Session(opener)
    results, memo = [], {}
    for ref in refs:
        k = _key(ref)
        if k in memo:
            r = dict(memo[k])
            r.update(id=ref["id"], raw=ref["raw"][:300], duplicate_of=memo[k]["id"])
        else:
            r = evaluate(ref, session)
            memo[k] = r
        results.append(r)
    counts = Counter(r["verdict"] for r in results)
    attention = [{"id": r["id"], "verdict": r["verdict"], "raw": r["raw"], "note": r["note"],
                  "differences": r["differences"]} for r in results if r["verdict"] in ATTENTION]
    attention.sort(key=lambda a: ATTENTION.index(a["verdict"]))
    unparsed = [r["id"] for r in results if not (r["parsed"]["title"] or r["parsed"]["doi"] or r["parsed"]["arxiv"])]
    if unparsed:
        coverage.append(f"{len(unparsed)} references could not be parsed into title or identifier and were searched "
                        "by raw text only: " + ", ".join(unparsed[:10]) + ("..." if len(unparsed) > 10 else ""))
    if counts.get("unverifiable"):
        coverage.append(f"{counts['unverifiable']} references are web pages, very short entries, or DOIs outside "
                        "Crossref/DataCite and could not be verified against an index.")
    if counts.get("lookup_failed"):
        coverage.append(f"{counts['lookup_failed']} references could not be checked because a service failed; "
                        "they are not verified.")
    if offline:
        coverage.append("Offline mode: references were extracted and parsed but not checked.")
    coverage.append("Books, theses, standards and grey literature are often missing from these indexes; "
                    "'not_found' for them is weak evidence.")
    log = session.log if session else []
    services = Counter(e["service"] for e in log)
    return {
        "file": str(path), "source_format": extracted["source"],
        "summary": {"references": len(results), "verdicts": dict(counts),
                    "needs_attention": len(attention)},
        "needs_attention": attention,
        "references": results,
        "coverage": coverage,
        "services_contacted": dict(services),
        "requests_log": log,
        "service_errors": session.errors if session else [],
        "disclaimer": DISCLAIMER,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify that cited references exist and match the real record.")
    ap.add_argument("file")
    ap.add_argument("--offline", action="store_true", help="extract and parse only; make no requests")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        report = build_report(Path(args.file), args.offline, args.limit)
    except rx.ExtractError as e:
        print(json.dumps({"error": e.code, **({"detail": e.detail} if e.detail else {})}))
        return 2
    except Exception:
        print(json.dumps({"error": "unreadable_file"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
