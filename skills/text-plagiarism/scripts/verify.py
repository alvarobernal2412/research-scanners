"""Open tier, second half: verify every search hit locally before it can become a finding.

A search backend only says "this document may contain the phrase". The verifier fetches the open text of the
hit (a public URL, no user text), recomputes the overlap locally, and only a verified hit is compared against
the whole document so the full copied passage is reported. Hits that only carry a snippet, or whose text could
not be fetched, are listed as unverified candidates and never become findings.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acquire import service_for_url as service_for_url  # noqa: E402,F401
from acquire import text_from_response  # noqa: E402
from approval import PlannedRequest  # noqa: E402
from attribute import doi_key  # noqa: E402
from compare import find_matches  # noqa: E402
from extract import ExtractError, Line, extract_bytes, jats_info  # noqa: E402
from findings import SourceRec, compare_sources, state_entries  # noqa: E402
from prepare import load_doc  # noqa: E402
from net import Response, cache_root  # noqa: E402
from open_tier import Candidate, load_open_state  # noqa: E402
from textnorm import GLUED_MAX_RATIO, glued_ratio, split_sentences, tokenize  # noqa: E402

VERIFY_MIN_RUN_WORDS = 8
VERIFY_MIN_FRACTION = 0.8
BOILERPLATE_MIN_SEARCH_HITS = 5
BOILERPLATE_INSIDE_FRACTION = 0.8
# Only a phrase search counts documents. OpenAlex search is stemmed and relevance-ranked, CORE and the web
# engines count loosely matching pages: those counts say nothing about how many documents contain the sentence.
DOC_COUNT_BACKENDS = frozenset({"europepmc"})


def _url_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def _all_hits(state: dict):
    for key, res in state.get("results", {}).items():
        q = state["queries"].get(key, {})
        for h in res.get("hits", []):
            yield key, q, res, h


def plan_verification(state: dict) -> list[PlannedRequest]:
    fetched = state.get("verify", {}).get("fetched", {})
    seen: set[str] = set()
    reqs = []
    for _, _, _, h in _all_hits(state):
        url = h.get("fulltext_url")
        if not url or h.get("full_text") or url in seen or url in fetched:
            continue
        seen.add(url)
        reqs.append(PlannedRequest(id="", service=service_for_url(url), method="GET", url=url, carries="public_url",
                                   purpose="fetch the open text of a search hit to verify the overlap locally",
                                   subject=f"hit:{_url_id(url)}"))
    return reqs


def _is_web_hit(state: dict, url: str) -> bool:
    return any(h.get("fulltext_url") == url and h.get("backend") == "brave" for _, _, _, h in _all_hits(state))


def apply_fetch(state: dict, req: PlannedRequest, resp: Response, workdir: Path) -> None:
    fetched = state.setdefault("verify", {}).setdefault("fetched", {})
    entry = {"subject": req.subject, "status": None, "text_path": None, "license": None, "error": resp.error}
    lines, reason = text_from_response(resp)
    if lines is None and reason == "landing_page_only" and _is_web_hit(state, req.url):
        try:       # a web page found by a search engine is the text itself: extract its visible text
            doc = extract_bytes(resp.body, "html")
            lines, reason = (doc.lines, None) if glued_ratio(tokenize(doc.lines)) <= GLUED_MAX_RATIO else (None, "low_text_quality")
        except ExtractError:
            reason = "no_text"
    if lines is None:
        entry["status"] = reason or "download_failed"
    else:
        entry["status"] = "ok"
        try:
            info = jats_info(resp.body) if b"<article" in resp.body[:8192].lower() else {}
        except Exception:  # noqa: BLE001 - metadata is a bonus
            info = {}
        entry["license"] = info.get("license")
        entry["title"] = info.get("title")
        store = cache_root() / "sources"
        store.mkdir(parents=True, exist_ok=True)
        path = store / (hashlib.sha256(req.url.encode("utf-8")).hexdigest() + ".json")
        path.write_text(json.dumps({"lines": [asdict(l) for l in lines], "url": req.url}, ensure_ascii=False),
                        encoding="utf-8")
        entry["text_path"] = str(path)
    fetched[req.url] = entry


def verify_sentence(candidate: Candidate, source_lines: list[Line]):
    """The best match of the candidate sentence in the source text, or None when it is not really there."""
    sent = [t.key for t in tokenize([Line(1, candidate.text)])]
    src = [t.key for t in tokenize(source_lines)]
    matches = find_matches(sent, src, min_words=VERIFY_MIN_RUN_WORDS)
    if not matches:
        return None
    best = max(matches, key=lambda m: m.longest_run)
    if best.longest_run >= VERIFY_MIN_RUN_WORDS or best.longest_run >= VERIFY_MIN_FRACTION * len(sent):
        return best
    return None


def _hit_lines(h: dict, fetched: dict) -> tuple[list[Line] | None, str | None, str | None]:
    """(lines, license from the fetched text, failure reason)."""
    if h.get("full_text"):
        return [Line(i, t) for i, t in enumerate(h["full_text"].splitlines(), 1)], None, None
    url = h.get("fulltext_url")
    if not url:
        return None, None, "snippet_only"
    f = fetched.get(url)
    if f is None:
        return None, None, "not_fetched"
    if f["status"] != "ok":
        return None, None, f"fetch_{f['status']}"
    data = json.loads(Path(f["text_path"]).read_text(encoding="utf-8"))
    return [Line(**l) for l in data["lines"]], f.get("license"), None


def open_findings(workdir: Path) -> dict:
    empty = {"findings": [], "informational": [], "boilerplate": [], "unverified_candidates": [],
             "sources_compared": []}
    state = load_open_state(workdir)
    if not state or not state.get("results"):
        return empty
    cands = {c["id"]: Candidate(**c) for c in state["selection"]["candidates"]}
    fetched = state.get("verify", {}).get("fetched", {})
    entries = state_entries(workdir)
    recs: dict[str, SourceRec] = {}
    unverified: list[dict] = []
    seen_unverified: set[tuple] = set()
    high_hit: dict[str, int] = {}
    verified: dict[str, list[str]] = {}
    for key, q, res, h in _all_hits(state):
        cand = cands.get(q.get("candidate"))
        if cand is None:
            continue
        if (res.get("hit_count") or 0) >= BOILERPLATE_MIN_SEARCH_HITS and q.get("backend", h.get("backend")) in DOC_COUNT_BACKENDS:
            high_hit[cand.id] = max(high_hit.get(cand.id, 0), res["hit_count"])
        lines, lic, reason = _hit_lines(h, fetched)
        if lines is None:
            tag = (cand.id, h.get("backend"), h.get("url") or h.get("doi") or h.get("title"))
            if tag not in seen_unverified:
                seen_unverified.add(tag)
                unverified.append({"backend": h.get("backend"), "title": h.get("title"), "url": h.get("url"),
                                   "doi": h.get("doi"), "snippet": h.get("snippet"), "candidate": cand.id,
                                   "passage": cand.text, "reason": reason, "notes": h.get("notes", [])})
            continue
        if verify_sentence(cand, lines) is None:
            continue
        rid = (h.get("doi") or h.get("fulltext_url") or h.get("url") or "").lower() or f"{h.get('backend')}:{key}"
        verified.setdefault(rid, []).append(cand.id)
        if rid in recs:
            continue
        keys = set()
        ref_n = None
        for i, e in enumerate(entries):
            if h.get("doi") and h["doi"].lower() in {e.doi, e.resolved_doi}:
                keys |= set(e.keys())
                ref_n = ref_n if ref_n is not None else (e.n if e.n is not None else i + 1)
        if h.get("doi"):
            keys.add(doi_key(h["doi"]))
        recs[rid] = SourceRec(id=rid, lines=lines, doi=h.get("doi"), title=h.get("title"),
                              url=h.get("url") or h.get("fulltext_url"), license=lic or h.get("license"),
                              found_via=f"open:{h.get('backend')}", source_keys=frozenset(keys), ref_n=ref_n)
    spans = _candidate_spans(workdir, cands)
    anchors = {rid: [spans[c] for c in ids if c in spans] for rid, ids in verified.items()}
    # 8 words suffice only where the hit verified a candidate sentence; elsewhere the usual 12-word default
    result = (compare_sources(workdir, list(recs.values()), anchors=anchors, anchor_min_words=VERIFY_MIN_RUN_WORDS,
                              with_spans=True) if recs else dict(empty))
    # a sentence that many independent documents contain is common phrasing, not a copy of one of them
    if high_hit:
        stock = {cid: spans[cid] for cid in high_hit if cid in spans}
        stock_tokens = {i for lo, hi in stock.values() for i in range(lo, hi)}
        for name in ("findings", "informational"):
            keep = []
            for f in result[name]:
                lo, hi = f["_span"]
                inside = sum(1 for i in range(lo, hi) if i in stock_tokens)
                if hi > lo and inside / (hi - lo) >= BOILERPLATE_INSIDE_FRACTION:
                    result["boilerplate"].append(f)       # the finding is (inside) the stock sentence itself
                else:
                    keep.append(f)
            result[name] = keep
        covered = [f["_span"] for f in result["boilerplate"]]
        for cid, n in high_hit.items():
            c = cands[cid]
            lo, hi = stock.get(cid, (0, 0))
            if not any(lo < e and s < hi for s, e in covered):
                result["boilerplate"].append({"id": "", "passage": c.text, "search_hit_count": n,
                                              "location": {"section": c.section_id, "line_start": c.line_start,
                                                           "line_end": c.line_end, "page": None},
                                              "note": "many independent documents contain this sentence"})
        for n, f in enumerate(result["boilerplate"], 1):
            f["id"] = f"b{n}"
        for name, prefix in (("findings", "f"), ("informational", "i")):
            for n, f in enumerate(result[name], 1):
                f["id"] = f"{prefix}{n}"
    for name in ("findings", "informational", "boilerplate"):
        for f in result[name]:
            f.pop("_span", None)
    result["unverified_candidates"] = unverified
    return result


def _candidate_spans(workdir: Path, cands: dict) -> dict[str, tuple[int, int]]:
    """Document token range of each candidate sentence."""
    doc, _, _ = load_doc(workdir)
    tokens = tokenize(doc.lines)
    sentences = split_sentences(doc.lines, tokens)
    by_pos = {(s.line_start, s.line_end, s.text): (s.start_tok, s.end_tok) for s in sentences}
    out = {}
    for cid, c in cands.items():
        span = by_pos.get((c.line_start, c.line_end, c.text))
        if span is None:       # the sentence list changed shape: fall back to the sentence on the same line
            span = next(((s.start_tok, s.end_tok) for s in sentences
                         if s.line_start == c.line_start and s.text.startswith(c.text[:30])), None)
        if span is not None:
            out[cid] = span
    return out
