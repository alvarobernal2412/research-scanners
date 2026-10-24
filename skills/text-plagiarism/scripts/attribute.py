"""Attribution of a matched span: quoted or not, cited or not.

Statuses: quoted_cited (info), cited_not_quoted (light), quoted_uncited (light), unattributed (flag).
Citations are recognised per line: numeric ([3], [5-7]), author-year ((Smith et al., 2020), Smith (2020)),
LaTeX \\cite variants and DOI / arXiv links.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import Line  # noqa: E402
from textnorm import Sentence, Token  # noqa: E402

QUOTED_MIN_FRACTION = 0.8


def numeric_key(n: int) -> str:
    return f"n:{n}"


def _strip_marks(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def author_year_key(surname: str, year: str) -> str:
    y = re.match(r"\d{4}", year.strip())
    return f"ay:{_strip_marks(surname).casefold().strip()}:{y.group() if y else year.strip()}"


def tex_key(key: str) -> str:
    return f"tex:{key.strip()}"


def doi_key(doi: str) -> str:
    return f"doi:{doi.strip().lower().rstrip('.,;:)]}>')}"


def arxiv_key(arxiv_id: str) -> str:
    base = re.sub(r"v\d+$", "", arxiv_id.strip().lower())
    return "arxiv:" + base


@dataclass
class Citation:
    line: int
    start: int
    keys: frozenset[str]


_NUM_BRACKET = re.compile(r"\[(\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?(?:\s*[,;]\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?)*\s*)\](?!\()")
_NAME = r"[A-Z\u00C0-\u017F][\w'\u2019\-\u00C0-\u017F]+"
_YEAR = r"(?:1[5-9]\d\d|20\d\d)[a-z]?"
_PAREN = re.compile(r"\(([^()]*?\b" + _YEAR + r"\b[^()]*)\)")
_NARRATIVE = re.compile(
    rf"({_NAME})(?:\s+(?:et\s+al\.?|and\s+{_NAME}|&\s+{_NAME}))?\s*\(\s*({_YEAR})\s*(?:[;,][^()]*)?\)")
_TEX_CITE = re.compile(r"\\(?:[A-Za-z]*cite[A-Za-z]*)\*?(?:\[[^\]]*\]){0,2}\{([^}]*)\}")
_DOI = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|\bdoi:\s*)(10\.\d{4,9}/[^\s\"<>]+)", re.I)
_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)|arXiv:\s*(\d{4}\.\d{4,5}(?:v\d+)?)", re.I)
_SKIP_WORDS = {"see", "cf", "e.g", "eg", "also", "compare", "for", "example", "in", "as", "by", "and", "figure", "table"}


def _expand_numbers(body: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,;]", body):
        part = part.strip()
        m = re.fullmatch(r"(\d+)\s*[-\u2013\u2014]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b <= a + 200:
                out.extend(range(a, b + 1))
            else:
                out.extend((a, b))
        elif part.isdigit():
            out.append(int(part))
    return out


def find_citations(lines: list[Line]) -> list[Citation]:
    found: list[Citation] = []
    for ln in lines:
        text = ln.text
        if not text.strip():
            continue
        for m in _NUM_BRACKET.finditer(text):
            keys = frozenset(numeric_key(n) for n in _expand_numbers(m.group(1)))
            if keys:
                found.append(Citation(ln.no, m.start(), keys))
        for m in _PAREN.finditer(text):
            for part in m.group(1).split(";"):
                ym = re.search(r"\b(" + _YEAR + r")\b", part)
                if not ym:
                    continue
                words = [w for w in re.findall(_NAME, part[:ym.start()]) if w.casefold().rstrip(".") not in _SKIP_WORDS]
                if words:
                    found.append(Citation(ln.no, m.start(), frozenset({author_year_key(words[0], ym.group(1))})))
        for m in _NARRATIVE.finditer(text):
            if m.group(1).casefold() in _SKIP_WORDS:
                continue
            found.append(Citation(ln.no, m.start(), frozenset({author_year_key(m.group(1), m.group(2))})))
        for m in _TEX_CITE.finditer(text):
            keys = frozenset(tex_key(k) for k in m.group(1).split(",") if k.strip())
            if keys:
                found.append(Citation(ln.no, m.start(), keys))
        for m in _DOI.finditer(text):
            found.append(Citation(ln.no, m.start(), frozenset({doi_key(m.group(1))})))
        for m in _ARXIV.finditer(text):
            found.append(Citation(ln.no, m.start(), frozenset({arxiv_key(m.group(1) or m.group(2))})))
    found.sort(key=lambda c: (c.line, c.start))
    return found


_OPEN_CLOSE = {"\u201c": "\u201d", "\u00ab": "\u00bb"}


def quote_mask(lines: list[Line], tokens: list[Token]) -> list[bool]:
    """True for tokens inside "…", “…” or «…» pairs within one paragraph, or on block-quote lines."""
    order = {l.no: i for i, l in enumerate(lines)}
    mask = [False] * len(tokens)
    # paragraphs: runs of non-blank lines
    paragraphs: list[list[Line]] = []
    cur: list[Line] = []
    for ln in lines:
        if ln.text.strip():
            cur.append(ln)
        elif cur:
            paragraphs.append(cur)
            cur = []
    if cur:
        paragraphs.append(cur)
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for para in paragraphs:
        straight: list[tuple[int, int]] = []
        stack: list[tuple[str, tuple[int, int]]] = []
        for ln in para:
            for col, ch in enumerate(ln.text):
                pos = (order[ln.no], col)
                if ch == '"':
                    straight.append(pos)
                elif ch in _OPEN_CLOSE:
                    stack.append((_OPEN_CLOSE[ch], pos))
                elif ch in _OPEN_CLOSE.values():
                    for j in range(len(stack) - 1, -1, -1):
                        if stack[j][0] == ch:
                            spans.append((stack[j][1], pos))
                            del stack[j:]
                            break
        for a, b in zip(straight[0::2], straight[1::2]):
            spans.append((a, b))   # an odd last quote has no partner and quotes nothing
    spans.sort()
    si = 0
    in_quote_lines = {l.no for l in lines if l.in_quote}
    for i, t in enumerate(tokens):
        if t.line in in_quote_lines and t.end_line in in_quote_lines:
            mask[i] = True
            continue
        start, end = (order[t.line], t.start), (order[t.end_line], t.end)
        for a, b in spans:
            if a < end and start < b:
                mask[i] = True
                break
            if a >= end:
                break
    return mask


@dataclass
class Attribution:
    status: str
    severity: str
    cites_other_source: bool


_STATUS = {"quoted_cited": "info", "cited_not_quoted": "light", "quoted_uncited": "light", "unattributed": "flag"}


def classify(span: tuple[int, int], tokens: list[Token], sentences: list[Sentence], mask: list[bool],
             citations: list[Citation], source_keys: frozenset[str]) -> Attribution:
    a, b = span
    n = max(1, b - a)
    quoted = sum(1 for m in mask[a:b] if m) / n >= QUOTED_MIN_FRACTION
    overlapping = [i for i, s in enumerate(sentences) if s.start_tok < b and s.end_tok > a]
    if overlapping:
        first, last = overlapping[0], overlapping[-1]
        t0 = tokens[sentences[first].start_tok]
        lo = (t0.line, t0.start)
        hi = None
        if last + 2 < len(sentences):
            t1 = tokens[sentences[last + 2].start_tok]
            hi = (t1.line, t1.start)
    else:
        t0 = tokens[a]
        lo, hi = (t0.line, t0.start), None
    cited = other = False
    for c in citations:
        pos = (c.line, c.start)
        if pos < lo or (hi is not None and pos >= hi):
            continue
        if c.keys & source_keys:
            cited = True
        elif c.keys:
            other = True
    if cited and quoted:
        status = "quoted_cited"
    elif cited:
        status = "cited_not_quoted"
    elif quoted:
        status = "quoted_uncited"
    else:
        status = "unattributed"
    return Attribution(status, _STATUS[status], other)
