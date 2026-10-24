"""Turn document-vs-source matches into findings, informational lines and boilerplate.

A finding is textual overlap plus how it is attributed (quoted / cited). It is evidence, not a verdict.
Excluded sections (references, acknowledgements, statements) and Markdown footnote definitions never yield
findings: their tokens are blanked before comparison.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acquire import SourceInfo, load_source_lines  # noqa: E402
from attribute import Attribution, arxiv_key, classify, doi_key, find_citations, quote_mask  # noqa: E402
from compare import Match, SourceIndex  # noqa: E402
from extract import Document, Line  # noqa: E402
from prepare import load_doc  # noqa: E402
from refs import RefEntry  # noqa: E402
from sections import Section, excluded_lines  # noqa: E402
from textnorm import Token, split_sentences, tokenize  # noqa: E402

HIGH_CONFIDENCE_WORDS = 20
BOILERPLATE_MIN_SOURCES = 3


@dataclass
class SourceRec:
    """A source to compare the document against (a downloaded reference, a corpus item, an open-tier hit)."""
    id: str
    lines: list[Line]
    doi: str | None = None
    title: str | None = None
    url: str | None = None
    license: str | None = None
    version: str | None = None
    oa_status: str | None = None
    found_via: str = "strict:reference"
    source_keys: frozenset[str] = frozenset()
    ref_n: int | None = None
    relation: str | None = None          # corpus items: "related" | "own"


def _section_of(sections: list[Section], line_no: int) -> str | None:
    for s in sections:
        if s.start_line <= line_no <= s.end_line:
            return s.id
    return None


def make_finding(doc: Document, sections: list[Section], tokens: list[Token], match: Match, source: SourceRec | SourceInfo,
                 attribution: Attribution, ref_n: int | None, kind: str | None = None) -> dict:
    span = tokens[match.doc_start:match.doc_end]
    first, last = span[0], span[-1]
    by_no = {l.no: l for l in doc.lines}
    sev = attribution.severity
    if kind == "self_overlap" and sev == "flag":
        sev = "light"                      # overlap with the user's own earlier work is never a flag
    finding = {
        "id": "",
        "passage": " ".join(t.raw or by_no[t.line].text[t.start:t.end] for t in span),
        "location": {"section": _section_of(sections, first.line), "line_start": first.line,
                     "line_end": last.end_line, "page": by_no[first.line].page},
        "source": {"doi": source.doi, "title": source.title, "url": source.url, "license": source.license,
                   "version": source.version, "oa_status": source.oa_status, "found_via": source.found_via},
        "overlap_words": match.matched_words,
        "longest_run": match.longest_run,
        "match_type": match.kind,
        "attribution": attribution.status,
        "severity": sev,
        "confidence": "high" if match.longest_run >= HIGH_CONFIDENCE_WORDS else "medium",
        "reference_entry": ref_n,
        "cites_other_source": attribution.cites_other_source,
    }
    if kind:
        finding["kind"] = kind
    return finding


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def compare_sources(workdir: Path, recs: list[SourceRec]) -> dict:
    doc, sections, _ = load_doc(workdir)
    tokens = tokenize(doc.lines)
    excluded = excluded_lines(doc, sections)
    keys = [t.key if t.line not in excluded else f"\x00{i}" for i, t in enumerate(tokens)]
    sentences = split_sentences(doc.lines, tokens)
    mask = quote_mask(doc.lines, tokens)
    citations = [c for c in find_citations(doc.lines) if c.line not in excluded]
    index = SourceIndex()
    by_id: dict[str, SourceRec] = {}
    for rec in recs:
        by_id[rec.id] = rec
        index.add(rec.id, [t.key for t in tokenize(rec.lines)])
    found = index.find(keys)
    entries: list[tuple[SourceRec, Match]] = [(by_id[sid], m) for sid, ms in found.items() for m in ms]
    entries.sort(key=lambda e: (e[1].doc_start, e[0].id))
    result = {"findings": [], "informational": [], "boilerplate": [], "sources_compared": [
        {"doi": r.doi, "title": r.title, "url": r.url, "license": r.license, "version": r.version,
         "found_via": r.found_via, "id": r.id} for r in recs]}
    for rec, m in entries:
        span = (m.doc_start, m.doc_end)
        sources_over = {r2.id for r2, m2 in entries if _overlap(span, (m2.doc_start, m2.doc_end)) >= 0.5 * (m.doc_end - m.doc_start)}
        att = classify(span, tokens, sentences, mask, citations, rec.source_keys)
        finding = make_finding(doc, sections, tokens, m, rec, att, rec.ref_n,
                               kind="self_overlap" if rec.relation == "own" else None)
        if len(sources_over) >= BOILERPLATE_MIN_SOURCES:
            result["boilerplate"].append(finding)
        elif att.status == "quoted_cited":
            result["informational"].append(finding)
        else:
            result["findings"].append(finding)
    for name, prefix in (("findings", "f"), ("informational", "i"), ("boilerplate", "b")):
        for n, f in enumerate(result[name], 1):
            f["id"] = f"{prefix}{n}"
    return result


def source_keys_for(info: SourceInfo, entries: list[RefEntry]) -> tuple[frozenset[str], int | None]:
    keys: set[str] = set()
    ref_n = None
    for i, e in enumerate(entries):
        same = (info.doi and info.doi in {e.doi, e.resolved_doi}) or (info.arxiv and e.arxiv and
                                                                      info.arxiv.split("v")[0] == e.arxiv.split("v")[0])
        if same:
            keys |= e.keys()
            if ref_n is None:
                ref_n = e.n if e.n is not None else i + 1
    if info.doi:
        keys.add(doi_key(info.doi))
    if info.arxiv:
        keys.add(arxiv_key(info.arxiv))
    return frozenset(keys), ref_n


def load_state(workdir: Path) -> dict:
    p = Path(workdir) / "state.json"
    if not p.is_file():
        return {"references": None, "sources": [], "rounds": []}
    return json.loads(p.read_text(encoding="utf-8"))


def state_entries(workdir: Path) -> list[RefEntry]:
    st = load_state(workdir)
    if st.get("references") is not None:
        return [RefEntry(**r) for r in st["references"]]
    return load_doc(workdir)[2]


def strict_source_recs(workdir: Path) -> list[SourceRec]:
    st = load_state(workdir)
    entries = state_entries(workdir)
    recs = []
    for d in st["sources"]:
        info = SourceInfo(**d)
        if info.status != "obtained" or not info.text_path:
            continue
        try:
            lines = load_source_lines(info)
        except (OSError, ValueError, KeyError):
            continue
        keys, ref_n = source_keys_for(info, entries)
        recs.append(SourceRec(id=info.key, lines=lines, doi=info.doi, title=info.title, url=info.url,
                              license=info.license, version=info.version, oa_status=info.oa_status,
                              found_via=info.found_via, source_keys=keys, ref_n=ref_n))
    return recs


def strict_findings(workdir: Path) -> dict:
    return compare_sources(workdir, strict_source_recs(workdir))
