"""Assemble the coverage-first JSON report from a run directory. No network.

    python -I report.py --workdir W

Key order: coverage, summary, findings, informational, boilerplate, unverified_candidates, disclosure, disclaimer.
Statuses: `findings`, `no_overlap_found_in_checked_sources`, `insufficient_coverage`. There is no verdict, no
score and no authorship claim; "no overlap found" is only ever said about the sources that could be checked.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import corpus  # noqa: E402
from acquire import SourceInfo  # noqa: E402
from common import configure_stdout, emit, fail  # noqa: E402
from findings import combined_findings, load_state, match_entry, state_entries  # noqa: E402
from open_tier import load_open_state  # noqa: E402
from prepare import load_doc  # noqa: E402
from sections import EXCLUDED_KINDS  # noqa: E402
from strict import source_key  # noqa: E402

MIN_FULLTEXT_REFS = 3
# a cited work that is known to have no open copy; every other unchecked work keeps the status from "no overlap"
UNOBTAINABLE_REASONS = frozenset({"paywalled", "no_oa_location", "landing_page_only", "only_supplementary_files"})
MIN_OPEN_QUERIED = 10

DISCLAIMER = ("Findings show textual overlap with the listed sources and how it is attributed. They are not a legal "
              "verdict on plagiarism or copyright, and no findings means only that nothing matched in the sources "
              "that could be checked.")

LIMITS_ALWAYS = [
    "Unpublished, private or unindexed sources cannot be detected.",
    "Paywalled sources cannot be checked unless an open copy exists or you supply the PDF with --corpus.",
    "Paraphrase and translation are not detected: only verbatim and near-verbatim overlap is found.",
    "The strict tier compares only against works in the reference list and in the corpus you provided.",
]
LIMITS_OPEN = {
    "openalex": "OpenAlex full-text search is stemmed and covers about 17% of works; in testing about 40% of true "
                "sentences were never found by it, so zero results mean not covered, not absent.",
    "europepmc": "Europe PMC covers open-access biomedical literature only.",
    "core": "CORE body-text coverage is unverified.",
    "openlibrary": "Open Library returns snippets only, so its hits are listed as unverified candidates.",
    "brave": "Web search finds only pages the search engine indexes; each hit page is verified locally.",
}
LIMIT_CORPUS = ("The user-supplied corpus is compared as given; overlap with works not in the corpus, references or "
                "searched indexes is not detected.")
NETWORK_ROUNDS_THAT_MUST_WORK = {"resolve", "locate", "zenodo", "search", "search2", "verify"}
SERVICE_FAILURES = {"http_429", "http_5xx", "timeout", "network_error", "budget_exhausted", "missing_key",
                    "redirect_blocked", "too_large"}


def _read_log(workdir: Path) -> list[dict]:
    p = workdir / "requests.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _entry_id(i: int, e) -> str:
    doi = e.doi or e.resolved_doi
    return source_key(doi, e.arxiv) if (doi or e.arxiv) else f"ref:{i}"


def _merge_open(base: dict, op: dict) -> dict:
    """Open-tier findings are added unless the same source already explains the same lines."""
    for name in ("findings", "informational"):
        for f in op.get(name, []):
            dup = any((g["source"]["doi"] and g["source"]["doi"] == f["source"]["doi"]) and
                      g["location"]["line_start"] <= f["location"]["line_end"] and
                      g["location"]["line_end"] >= f["location"]["line_start"]
                      for k in ("findings", "informational") for g in base[k])
            if not dup:
                base[name].append(f)
    base["boilerplate"].extend(op.get("boilerplate", []))
    base["unverified_candidates"] = op.get("unverified_candidates", [])
    seen = {(s.get("doi"), s.get("url"), s.get("title")) for s in base["sources_compared"]}
    for s in op.get("sources_compared", []):
        if (s.get("doi"), s.get("url"), s.get("title")) not in seen:
            base["sources_compared"].append(s)
    return base


def _renumber(result: dict) -> None:
    for name, prefix in (("findings", "f"), ("informational", "i"), ("boilerplate", "b")):
        result[name].sort(key=lambda f: (f.get("location", {}).get("line_start", 0), f.get("passage", "")))
        for n, f in enumerate(result[name], 1):
            f["id"] = f"{prefix}{n}"


def build_report(workdir: Path) -> dict:
    workdir = Path(workdir)
    doc, sections, doc_refs = load_doc(workdir)
    state = load_state(workdir)
    entries = state_entries(workdir)
    infos = [SourceInfo(**d) for d in state.get("sources", [])]
    by_key = {i.key: i for i in infos}
    log = _read_log(workdir)
    open_state = load_open_state(workdir)
    items = corpus.item_table(workdir)
    result = combined_findings(workdir) if (infos or items) else {
        "findings": [], "informational": [], "boilerplate": [], "sources_compared": []}
    result["unverified_candidates"] = []
    if open_state and open_state.get("results"):
        import verify

        result = _merge_open(result, verify.open_findings(workdir))
    _renumber(result)

    # ------------------------------------------------------------ references
    unresolved, obtained, not_obtained = [], [], []
    resolved = 0
    for i, e in enumerate(entries):
        n = e.n if e.n is not None else i + 1
        ident = bool(e.doi or e.resolved_doi or e.arxiv)
        if ident and e.resolution in ("doi_in_text", "arxiv_in_text", "crossref_accepted"):
            resolved += 1
        else:
            unresolved.append({"n": n, "text": e.text[:300], "resolution": e.resolution})
            continue
        info = by_key.get(_entry_id(i, e))
        doi = e.doi or e.resolved_doi
        if info is not None and info.status == "obtained":
            obtained.append({"n": n, "doi": doi, "title": info.title or e.title, "license": info.license,
                             "version": info.version, "url": info.url})
        else:
            reason = info.reason if info is not None and info.status == "not_obtained" else "not_attempted"
            not_obtained.append({"n": n, "doi": doi, "title": (info.title if info else None) or e.title,
                                 "reason": reason or "download_failed"})

    # ------------------------------------------------------------ rounds, backends, tiers
    strict_rounds = {r["round"] for r in state.get("rounds", [])}
    open_rounds = {r["round"] for r in (open_state or {}).get("rounds", [])}
    tiers = [t for t, ran in (("strict", bool(strict_rounds)), ("open", bool(open_rounds))) if ran]
    backend_stats: dict[str, dict] = {}
    for e in log:
        b = backend_stats.setdefault(e["service"], {"service": e["service"], "sent": 0, "ok": 0, "cached": 0,
                                                    "errors": Counter()})
        if e.get("from_cache"):
            b["cached"] += 1
        else:
            b["sent"] += 1
        if e.get("error"):
            b["errors"][e["error"]] += 1
        elif e.get("status") == 200:
            b["ok"] += 1
    backends = [{**b, "errors": dict(b["errors"])} for b in backend_stats.values()]

    # ------------------------------------------------------------ open tier
    open_block = {"ran": bool(open_rounds), "sections_selected": [], "sentences_eligible": 0,
                  "sentences_queried": 0, "excluded_by_filter": {}, "not_queried_over_cap": {},
                  "queries_failed": 0}
    open_error_free_backend = False
    if open_state and open_state.get("selection"):
        sel = open_state["selection"]
        open_block["sections_selected"] = sel["sections"]
        open_block["sentences_eligible"] = len(sel["candidates"]) + sum(sel.get("over_cap", {}).values())
        open_block["excluded_by_filter"] = dict(Counter(c["excluded"] for c in sel.get("excluded", [])))
        open_block["not_queried_over_cap"] = sel.get("over_cap", {})
        ok_cands = set()
        per_backend_err: dict[str, int] = {}
        per_backend_ok: dict[str, int] = {}
        for key, res in open_state.get("results", {}).items():
            q = open_state["queries"][key]
            if res.get("error"):
                open_block["queries_failed"] += 1
                per_backend_err[q["backend"]] = per_backend_err.get(q["backend"], 0) + 1
            else:
                ok_cands.add(q["candidate"])
                per_backend_ok[q["backend"]] = per_backend_ok.get(q["backend"], 0) + 1
        open_block["sentences_queried"] = len(ok_cands)
        open_error_free_backend = any(b not in per_backend_err for b in per_backend_ok)

    # ------------------------------------------------------------ corpus
    corpus_block = {"provided": [], "compared": 0, "not_loaded": []}
    corpus_ids: set[str] = set()
    corpus_checked: set[str] = set()
    for it in items:
        corpus_block["provided"].append({k: it.get(k) for k in ("id", "name", "kind", "status", "reason", "words",
                                                                 "relation")})
        if it["status"] == "loaded":
            corpus_block["compared"] += 1
        elif it["status"] != "pending_fetch":
            corpus_block["not_loaded"].append({k: it.get(k) for k in ("id", "name", "status", "reason")})
        else:
            corpus_block["not_loaded"].append({"id": it["id"], "name": it["name"], "status": "pending_fetch",
                                               "reason": "the strict rounds did not fetch it yet"})
        if it["status"] in ("same_as_document",):
            continue
        if it["kind"] == "identifier":
            ident = (it.get("doi") or "").lower() or (it["identifier"] or "")
            if ident.lower().startswith("arxiv:"):
                ident = "arxiv:" + ident.split(":", 1)[1].lower().split("v")[0]
            matched = ident
        else:
            ref_doi = None
            if it["status"] == "loaded":
                class _I:      # match_entry works on the item fields
                    doi, title, authors, year = it.get("doi"), it.get("title"), it.get("authors"), it.get("year")
                ref_doi = match_entry(_I, entries)[2]
            matched = (ref_doi or it.get("doi") or "").lower() or f"corpus:{it['id']}"
        corpus_ids.add(matched)
        if it["status"] == "loaded":
            corpus_checked.add(matched)

    ref_ids = {_entry_id(i, e) for i, e in enumerate(entries)}
    checked_ids = {_entry_id(i, e) for i, e in enumerate(entries)
                   if by_key.get(_entry_id(i, e)) is not None and by_key[_entry_id(i, e)].status == "obtained"}
    checked_ids |= corpus_checked
    all_ids = ref_ids | corpus_ids
    checked_n, total_n = len(checked_ids & all_ids), len(all_ids)

    # ------------------------------------------------------------ status
    failures = []
    if "glued_words" in doc.warnings:
        failures.append("the document text has words glued together (PDF extraction), so matching is unreliable")
    for e in log:
        if e.get("round") in NETWORK_ROUNDS_THAT_MUST_WORK and e.get("error") in SERVICE_FAILURES:
            failures.append(f"a {e['service']} request failed ({e['error']}) in the {e['round']} round")
            break
    if any(u["resolution"] == "backend_error" for u in unresolved):
        failures.append("some references could not be resolved because the service failed")
    if open_block["queries_failed"]:
        failures.append(f"{open_block['queries_failed']} open-tier queries failed")
    if corpus_block["not_loaded"]:
        failures.append(f"{len(corpus_block['not_loaded'])} corpus item(s) could not be loaded or fetched")
    definitive = {_entry_id(i, e) for i, e in enumerate(entries)
                  if by_key.get(_entry_id(i, e)) is not None and by_key[_entry_id(i, e)].status == "not_obtained"
                  and by_key[_entry_id(i, e)].reason in UNOBTAINABLE_REASONS}
    unchecked_obtainable = all_ids - checked_ids - definitive      # could have been checked and was not
    over_cap = sum(open_block["not_queried_over_cap"].values())
    over_cap += sum((open_state or {}).get("selection", {}).get("backend_over_cap", {}).values())
    enough_refs = checked_n >= MIN_FULLTEXT_REFS and total_n > 0 and not unchecked_obtainable
    enough_open = (open_block["sentences_queried"] >= MIN_OPEN_QUERIED and open_error_free_backend
                   and over_cap == 0 and not unchecked_obtainable)
    if result["findings"]:
        status = "findings"
    elif (enough_refs or enough_open) and not failures:
        status = "no_overlap_found_in_checked_sources"
    else:
        status = "insufficient_coverage"
    reasons = list(failures)
    if checked_n < total_n or status == "insufficient_coverage":
        reasons.append(f"only {checked_n} of {total_n} cited or supplied sources were checked"
                       + ("" if open_block["ran"] else " and the open tier was not run"))
    if over_cap:
        reasons.append(f"{over_cap} eligible sentence request(s) were not queried because of the cap")

    # ------------------------------------------------------------ sections
    sec_rows = []
    queried_sections = set(open_block["sections_selected"]) if open_block["sentences_queried"] else set()
    for s in sections:
        excluded = s.kind in EXCLUDED_KINDS
        sec_rows.append({"id": s.id, "heading": s.heading, "kind": s.kind,
                         "checked_strict": bool(checked_n) and not excluded,
                         "checked_open": s.id in queried_sections and not excluded,
                         "excluded_reason": f"{s.kind} are never compared" if excluded else None})

    limits = list(LIMITS_ALWAYS)
    if open_rounds:
        used = {b for b in (open_state or {}).get("selection", {}).get("backends", [])}
        limits += [LIMITS_OPEN[b] for b in ("openalex", "europepmc", "core", "openlibrary", "brave") if b in used]
    if items:
        limits.append(LIMIT_CORPUS)
    pages = [l.page for l in doc.lines if l.page is not None]

    services = sorted({e["service"] for e in log})
    identities = sorted({e["identity"] for e in log if e.get("identity")})
    coverage = {
        "status": status, "status_reasons": reasons, "tiers_used": tiers,
        "extraction": {"fmt": doc.fmt, "pages": max(pages) if pages else None, "warnings": doc.warnings},
        "sections": sec_rows,
        "references": {"total": len(entries), "resolved": resolved, "unresolved": unresolved,
                       "full_text_obtained": obtained, "full_text_not_obtained": not_obtained,
                       "checked_of_total": {"checked": checked_n, "total": total_n}},
        "open": open_block, "backends": backends, "corpus": corpus_block, "limits": limits,
    }
    by_sev = Counter(f["severity"] for f in result["findings"])
    summary = {"findings_by_severity": {"flag": by_sev.get("flag", 0), "light": by_sev.get("light", 0)},
               "informational": len(result["informational"]), "boilerplate": len(result["boilerplate"]),
               "unverified_candidates": len(result["unverified_candidates"]),
               "sources_compared": len(result["sources_compared"]),
               "corpus_findings": sum(1 for f in result["findings"] + result["informational"]
                                      if f["source"].get("found_via") == "user_corpus")}
    disclosure = {"tiers_used": tiers, "services_contacted": services, "identities_used": identities,
                  "requests_sent": [{"service": e["service"], "tier": e["tier"], "round": e["round"], "url": e["url"],
                                     "text_sent": e.get("text_sent"), "status": e.get("status"),
                                     "error": e.get("error"), "from_cache": e.get("from_cache", False)} for e in log]}
    return {"coverage": coverage, "summary": summary, "findings": result["findings"],
            "informational": result["informational"], "boilerplate": result["boilerplate"],
            "unverified_candidates": result["unverified_candidates"], "disclosure": disclosure,
            "disclaimer": DISCLAIMER}


def main(argv: list[str] | None = None) -> int:
    import argparse

    configure_stdout()
    ap = argparse.ArgumentParser(prog="report.py")
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args(argv)
    wd = Path(args.workdir)
    if not (wd / "doc.json").is_file():
        return fail("workdir_missing", str(wd))
    emit(build_report(wd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
