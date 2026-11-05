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
from attribute import _TEX_CITE, arxiv_key, author_year_key, doi_key, numeric_key, tex_key  # noqa: E402
from extract import Document  # noqa: E402
from net import Response  # noqa: E402
from sections import Section  # noqa: E402

RESOLVE_MIN_SCORE = 70
RESOLVE_MIN_MARGIN = 20
CROSSREF_URL = "https://api.crossref.org/works"
MAX_REF_QUERY_CHARS = 400

RESOLUTIONS = ("doi_in_text", "arxiv_in_text", "crossref_accepted", "crossref_ambiguous", "crossref_low_score",
               "not_sent_unpublished", "not_sent_body_overlap", "backend_error", "not_attempted")


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


# ------------------------------------------------- LaTeX: external .bib / .bbl bibliography

_BIB_FILES = re.compile(r"\\(?:bibliography|addbibresource)\*?(?:\[[^\]]*\])?\{([^}]*)\}")
_BIB_START = re.compile(r"@(\w+)\s*\{\s*([^,\s{}]*)\s*,")
_ACCENT = re.compile(r"\\[`'^\"~=.](?:\{(\w)\}|(\w))")
_ACCENTED = {'"': "̈", "'": "́", "`": "̀", "^": "̂", "~": "̃"}


def _tex_clean(value: str) -> str:
    def accent(m: re.Match) -> str:
        return unicodedata.normalize("NFC", (m.group(1) or m.group(2)) + _ACCENTED.get(m.group(0)[1], ""))

    value = _ACCENT.sub(accent, value).replace("\\&", "&").replace("~", " ").replace("--", "\u2013")
    value = re.sub(r"\\[A-Za-z]+\*?", "", value)
    return re.sub(r"\s+", " ", re.sub(r"[{}]", "", value)).strip()


def _bib_value(text: str, i: int) -> tuple[str, int]:
    """The value starting at text[i] (braced, quoted or bare) and the index after it."""
    if text[i] == "{":
        depth, j = 1, i + 1
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        return text[i + 1:j - 1], j
    if text[i] == '"':
        j = i + 1
        while j < len(text) and text[j] != '"':
            j += 2 if text[j] == "\\" else 1
        return text[i + 1:j], j + 1
    m = re.compile(r"[^,}\s]*").match(text, i)
    return m.group(), m.end()


def parse_bib_entries(text: str) -> list[dict]:
    """Every entry of a .bib file, with or without a public identifier."""
    out = []
    for m in _BIB_START.finditer(text):
        if m.group(1).lower() in ("comment", "string", "preamble"):
            continue
        fields, i = {}, m.end()
        while i < len(text):
            f = re.compile(r"\s*(\w+)\s*=\s*").match(text, i)
            if not f:
                if text[i:i + 1].isspace() or text[i:i + 1] == ",":
                    i += 1
                    continue
                break
            value, i = _bib_value(text, f.end()) if f.end() < len(text) else ("", f.end())
            fields[f.group(1).lower()] = value
        authors = [a.strip() for a in re.split(r"\s+and\s+", _tex_clean(fields.get("author", ""))) if a.strip()]
        rec = {"key": m.group(2), "title": _tex_clean(fields.get("title", "")) or None, "authors": authors,
               "venue": _tex_clean(fields.get("journal") or fields.get("booktitle") or fields.get("publisher") or "") or None,
               "year": (re.search(r"\d{4}", fields.get("year", "")) or [None])[0]}
        raw_doi = _DOI.search(fields.get("doi", "") or fields.get("url", ""))
        if raw_doi:
            rec["doi"] = _clean_doi(raw_doi.group())
        eprint = fields.get("eprint", "").strip()
        if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", eprint):
            rec["arxiv"] = eprint.split("v")[0]
        url = fields.get("url", "").strip()
        if url.startswith("http") and not raw_doi:
            rec["url"] = url
        out.append(rec)
    return out


def _bib_entry_to_ref(rec: dict, line: int) -> RefEntry:
    surnames = [a.split(",")[0].strip() if "," in a else a.split()[-1] for a in rec["authors"]]
    names = ", ".join(rec["authors"])
    text = f"{names}: {rec['title'] or ''}. {rec['venue'] or ''} ({rec['year'] or ''})."
    entry = RefEntry(n=None, text=re.sub(r"\s+", " ", text).strip(), line_start=line, line_end=line,
                     doi=rec.get("doi"), arxiv=rec.get("arxiv"), url=rec.get("url"),
                     first_author=surnames[0] if surnames else None, year=rec["year"], tex_key=rec["key"],
                     title=None)
    if entry.doi:
        entry.resolution, entry.resolved_doi = "doi_in_text", entry.doi
    elif entry.arxiv:
        entry.resolution = "arxiv_in_text"
    elif _UNPUBLISHED.search(entry.text):
        entry.resolution = "not_sent_unpublished"
    return entry


def bibliography_from_tex(doc: Document, path: Path) -> tuple[list[RefEntry], list[str]]:
    """References of a .tex file that uses \bibliography / \addbibresource: the .bib entries whose keys
    the text cites (all of them after \nocite{*}), else the sibling .bbl. Returns (entries, notes)."""
    if doc.fmt != "tex":
        return [], []
    cited: dict[str, int] = {}
    names: list[str] = []
    nocite_all = False
    for ln in doc.lines:
        for m in _TEX_CITE.finditer(ln.text):
            for k in (k.strip() for k in m.group(1).split(",")):
                if k == "*":
                    nocite_all = True
                elif k:
                    cited.setdefault(k, ln.no)
        for m in _BIB_FILES.finditer(ln.text):
            names += [n.strip() for n in m.group(1).split(",") if n.strip()]
    path, notes, records = Path(path), [], {}
    for name in names:
        f = path.parent / (name if name.lower().endswith(".bib") else name + ".bib")
        try:
            for rec in parse_bib_entries(f.read_text(encoding="utf-8", errors="replace")):
                records.setdefault(rec["key"], rec)
        except OSError:
            notes.append(f"bib_file_not_found:{f.name}")
    if records:
        keys = list(records) if nocite_all else [k for k in records if k in cited]
        notes += [f"cited_key_not_in_bib:{k}" for k in cited if k not in records]
        return [_bib_entry_to_ref(records[k], cited.get(k, 0)) for k in sorted(keys, key=lambda k: cited.get(k, 1 << 30))], notes
    bbl = path.with_suffix(".bbl")
    if bbl.is_file():
        rows = [(i, _tex_clean(t) if not _BIBITEM.match(t.strip()) else t)
                for i, t in enumerate(bbl.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
                if t.strip() and not t.strip().startswith(("\\begin{thebib", "\\end{thebib"))]
        return _split_bibitems(rows), notes
    return [], notes


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
