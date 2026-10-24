"""Bibliography extraction and Crossref resolution.

Reference-list strings are published bibliographic data and may be sent in the strict tier. Entries that
are unpublished ("in preparation", "personal communication", ...) are never sent. An entry that already
carries a DOI or arXiv id needs no Crossref request.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval import PlannedRequest  # noqa: E402
from attribute import arxiv_key, author_year_key, doi_key, numeric_key, tex_key  # noqa: E402
from extract import Document  # noqa: E402
from net import Response  # noqa: E402
from sections import Section  # noqa: E402

RESOLVE_MIN_SCORE = 70
RESOLVE_MIN_MARGIN = 20
CROSSREF_URL = "https://api.crossref.org/works"
MAX_REF_QUERY_CHARS = 400

RESOLUTIONS = ("doi_in_text", "arxiv_in_text", "crossref_accepted", "crossref_ambiguous", "crossref_low_score",
               "not_sent_unpublished", "backend_error", "not_attempted")


@dataclass
class RefEntry:
    n: int | None
    text: str
    line_start: int
    line_end: int
    doi: str | None = None
    arxiv: str | None = None
    url: str | None = None
    first_author: str | None = None
    year: str | None = None
    tex_key: str | None = None
    resolved_doi: str | None = None
    title: str | None = None
    resolution: str = "not_attempted"

    def keys(self) -> frozenset[str]:
        out: set[str] = set()
        if self.n is not None:
            out.add(numeric_key(self.n))
        if self.first_author and self.year:
            out.add(author_year_key(self.first_author, self.year))
        if self.tex_key:
            out.add(tex_key(self.tex_key))
        for d in (self.doi, self.resolved_doi):
            if d:
                out.add(doi_key(d))
        if self.arxiv:
            out.add(arxiv_key(self.arxiv))
        return frozenset(out)


_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")
_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)|arXiv:\s*(\d{4}\.\d{4,5}(?:v\d+)?)", re.I)
_URL = re.compile(r"https?://[^\s\"<>]+")
_UNPUBLISHED = re.compile(r"in preparation|unpublished|\bsubmitted\b|under review|forthcoming|personal communication|"
                          r"\bmanuscript\b|in press\s*\(unpub|private communication", re.I)
_NAME = r"[A-Z\u00C0-\u017F][\w'\u2019\-\u00C0-\u017F]+"
_BRACKET = re.compile(r"^\s*\[(\d+)\]\s*")
_DOTNUM = re.compile(r"^\s*(\d{1,3})[.)]\s+(?=\S)")
_BULLET = re.compile(r"^\s*[-*+]\s+")
_BIBITEM = re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]*)\}\s*")
_PAGE_HEADER = re.compile(r"^\s*\d{1,4}\s+[A-Z]\.[^\n]{0,60}\bet al\.?\s*$|^\s*\d{1,4}\s*$")
_AUTHOR_START = re.compile(rf"^(?:{_NAME}(?:\s{_NAME})*,\s+[A-Z]\.|[A-Z]\.\s?(?:[A-Z]\.\s?)*{_NAME},)")


def _clean_doi(raw: str) -> str:
    return raw.rstrip(".,;:)]}>'\"").lower()


def _join(lines: list[str]) -> str:
    out = ""
    for part in (p.strip() for p in lines):
        if not part:
            continue
        if not out:
            out = part
        elif out.endswith("-") and part[:1].islower():
            out = out[:-1] + part                       # hyphenation at the line end
        elif out.endswith(("/", "-")) and (part[:1].isdigit() or "://" in out or "10." in out):
            out += part                                  # a DOI or URL broken over two lines
        else:
            out += " " + part
    return re.sub(r"\s+", " ", out).strip()


def _surname_and_year(text: str) -> tuple[str | None, str | None]:
    body = text
    body = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", body)
    before = re.split(r"[,:]", body, maxsplit=1)[0].strip()
    surname = None
    if re.fullmatch(rf"{_NAME}\s+[A-Z]{{1,3}}", before):           # "Smith JK, Lee K."
        surname = before.split()[0]
    elif re.match(rf"^(?:[A-Z]\.\s?)+{_NAME}", before):          # "J. K. Smith, A. Lee"
        surname = before.split()[-1]
    elif re.match(rf"^(?:[a-z]+\s)*{_NAME}(?:\s{_NAME})*$", before) and len(before.split()) <= 5:
        surname = before.split()[-1]                              # "Van Suetendael, J." / "van der Aalst, W."
    ym = re.search(r"[(\s,.]((?:1[5-9]|20)\d\d[a-z]?)[)\s,.:;]", body + " ")
    year = ym.group(1) if ym else None
    if surname:
        surname = surname.strip(".,")
    return surname, year


def _build(label: int | None, lines: list[tuple[int, str]], key: str | None = None) -> RefEntry:
    text = _join([t for _, t in lines])
    entry = RefEntry(n=label, text=text, line_start=lines[0][0], line_end=lines[-1][0], tex_key=key)
    m = _DOI.search(text)
    if m:
        entry.doi = _clean_doi(m.group())
    a = _ARXIV.search(text)
    if a:
        entry.arxiv = a.group(1) or a.group(2)
    for u in _URL.finditer(text):
        if "doi.org" not in u.group() and "arxiv.org" not in u.group():
            entry.url = u.group().rstrip(".,;:)]}>'\"")
            break
    entry.first_author, entry.year = _surname_and_year(text)
    if entry.doi:
        entry.resolution, entry.resolved_doi = "doi_in_text", entry.doi
    elif entry.arxiv:
        entry.resolution = "arxiv_in_text"
    elif _UNPUBLISHED.search(text):
        entry.resolution = "not_sent_unpublished"
    return entry


def _strip_label(text: str) -> str:
    return _BRACKET.sub("", _DOTNUM.sub("", _BULLET.sub("", text, count=1), count=1), count=1)


def parse_references(doc: Document, sections: list[Section]) -> list[RefEntry]:
    entries: list[RefEntry] = []
    for sec in sections:
        if sec.kind != "references":
            continue
        rows = [(l.no, l.text) for l in doc.lines if sec.start_line <= l.no <= sec.end_line]
        if rows and (rows[0][1].strip() == sec.heading or re.match(r"\\begin\{thebibliography\}", rows[0][1])):
            rows = rows[1:]
        rows = [(no, t) for no, t in rows if not _PAGE_HEADER.match(t) and not t.strip().startswith("\\end{thebib")]
        entries.extend(_split(rows))
    return entries


def _split(rows: list[tuple[int, str]]) -> list[RefEntry]:
    nonblank = [(no, t) for no, t in rows if t.strip()]
    if not nonblank:
        return []
    texts = [t for _, t in nonblank]
    if sum(1 for t in texts if _BIBITEM.match(t.strip())) >= 1:
        return _split_bibitems(nonblank)
    if sum(1 for t in texts if _BRACKET.match(t)) >= 2 or _BRACKET.match(texts[0]):
        return _split_numbered(nonblank, _BRACKET)
    first = _DOTNUM.match(texts[0])
    if _sequential(texts) >= 2 or (first and int(first.group(1)) == 1):
        return _split_numbered(nonblank, _DOTNUM)
    if sum(1 for t in texts if _BULLET.match(t)) >= 2:
        return _split_bullets(nonblank)
    return _split_blocks(rows)


def _sequential(texts: list[str]) -> int:
    count, last = 0, None
    for t in texts:
        m = _DOTNUM.match(t)
        if m and (last is None or int(m.group(1)) == last + 1):
            last = int(m.group(1))
            count += 1
    return count


def _split_numbered(rows, rx) -> list[RefEntry]:
    groups: list[tuple[int, list[tuple[int, str]]]] = []
    last = None
    for no, t in rows:
        m = rx.match(t)
        accept = bool(m) and (rx is _BRACKET or last is None or int(m.group(1)) == last + 1)
        if accept:
            last = int(m.group(1))
            groups.append((last, [(no, t[m.end():])]))
        elif groups:
            groups[-1][1].append((no, t))
    return [_build(n, lines) for n, lines in groups]


def _split_bullets(rows) -> list[RefEntry]:
    groups: list[list[tuple[int, str]]] = []
    for no, t in rows:
        m = _BULLET.match(t)
        if m:
            groups.append([(no, t[m.end():])])
        elif groups:
            groups[-1].append((no, t))
    return [_build(None, g) for g in groups]


def _split_bibitems(rows) -> list[RefEntry]:
    groups: list[tuple[str, list[tuple[int, str]]]] = []
    for no, t in rows:
        m = _BIBITEM.match(t.strip())
        if m:
            groups.append((m.group(1), [(no, t.strip()[m.end():])]))
        elif groups and not t.strip().startswith("\\end{"):
            groups[-1][1].append((no, t))
    return [_build(None, g, key=k) for k, g in groups]


def _split_blocks(rows) -> list[RefEntry]:
    has_blank = any(not t.strip() for _, t in rows[1:-1])
    groups: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    for no, t in rows:
        if not t.strip():
            if cur:
                groups.append(cur)
                cur = []
            continue
        starts = bool(_AUTHOR_START.match(t.strip()))
        prev = cur[-1][1].rstrip() if cur else ""
        if cur and not has_blank and starts and prev.endswith((".", ")", "\"", "\u201d")):
            groups.append(cur)
            cur = []
        cur.append((no, t))
    if cur:
        groups.append(cur)
    return [_build(None, g) for g in groups]


# ----------------------------------------------------------------- Crossref

def query_text(entry: RefEntry) -> str:
    return _strip_label(entry.text)[:MAX_REF_QUERY_CHARS].strip()


def plan_resolution(entries: list[RefEntry]) -> list[PlannedRequest]:
    import os

    identity = "TEXT_PLAGIARISM_MAILTO" if os.environ.get("TEXT_PLAGIARISM_MAILTO", "").strip() else None
    reqs = []
    for i, e in enumerate(entries):
        if e.resolution != "not_attempted":
            continue
        q = query_text(e)
        if len(q) < 15:
            continue
        url = CROSSREF_URL + "?" + urllib.parse.urlencode({"query.bibliographic": q, "rows": "2"},
                                                          quote_via=urllib.parse.quote)
        reqs.append(PlannedRequest(id="", service="crossref", method="GET", url=url, carries="reference_string",
                                   purpose="resolve a reference-list entry to a DOI", subject=f"ref:{i}",
                                   identity=identity))
    return reqs


def apply_resolution(entries: list[RefEntry], responses: dict[str, Response]) -> list[RefEntry]:
    for subject, resp in responses.items():
        if not subject.startswith("ref:"):
            continue
        i = int(subject[4:])
        if not 0 <= i < len(entries):
            continue
        e = entries[i]
        if resp.error or resp.status != 200:
            e.resolution = "backend_error"
            continue
        try:
            items = json.loads(resp.body.decode("utf-8"))["message"]["items"]
        except (ValueError, KeyError, TypeError):
            e.resolution = "backend_error"
            continue
        if not items:
            e.resolution = "crossref_low_score"
            continue
        top = float(items[0].get("score") or 0)
        second = float(items[1].get("score") or 0) if len(items) > 1 else 0.0
        if top < RESOLVE_MIN_SCORE:
            e.resolution = "crossref_low_score"
        elif top - second < RESOLVE_MIN_MARGIN:
            e.resolution = "crossref_ambiguous"
        else:
            e.resolution = "crossref_accepted"
            e.resolved_doi = str(items[0].get("DOI", "")).lower() or None
            titles = items[0].get("title") or []
            e.title = unicodedata.normalize("NFKC", re.sub(r"<[^>]+>", "", titles[0])).strip() if titles else None
    return entries
