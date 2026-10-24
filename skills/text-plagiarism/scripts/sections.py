"""Section detection: split a document into sections and mark the ones that must never yield findings.

Kinds: front (before the first heading), body, references, acknowledgements, statements, appendix.
Lines of the excluded kinds (references, acknowledgements, statements) and Markdown footnote
definitions are excluded from comparison and from open-tier queries.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import Document  # noqa: E402

EXCLUDED_KINDS = frozenset({"references", "acknowledgements", "statements"})

_NUM = r"(?:(?:\d+(?:\.\d+)*\.?|[IVX]+\.|[A-Z]\.\d+)\s+)?"
_KNOWN = [
    ("references", r"references?|bibliography|works cited|literature cited|reference list|cited literature"),
    ("acknowledgements", r"acknowledge?ments?|acknowledging"),
    ("statements", r"funding(?: statement| information)?|data availability(?: statement)?|"
                   r"availability of data(?: and materials?)?|conflicts? of interests?|competing interests?|"
                   r"declaration of competing interests?|author contributions?|ethics(?: statement)?|"
                   r"declarations?|disclosures?|code availability|authors?'? contributions?|financial disclosure"),
    ("appendix", r"appendix(?:\s+[A-Z0-9]+)?(?:[:.\s].*)?|appendices|supplementary (?:material|information)s?"),
]
_KNOWN_RE = [(k, re.compile(rf"^{_NUM}(?:{p})\s*[:.]?\s*$", re.I)) for k, p in _KNOWN]
_NUMBERED = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+[A-Z]")
_BIB_BEGIN = re.compile(r"\\begin\{thebibliography\}")
_BIB_END = re.compile(r"\\end\{thebibliography\}")
_FOOTNOTE_DEF = re.compile(r"^\[\^[^\]]+\]:")
_DEFAULT_OPEN = re.compile(r"\b(introduction|background|related work|literature|state of the art|"
                           r"prior work|previous work)\b", re.I)


@dataclass
class Section:
    id: str
    heading: str
    kind: str
    start_line: int
    end_line: int
    words: int


def _known_kind(text: str) -> str | None:
    t = text.strip()
    if not t or len(t.split()) > 8:
        return None
    for kind, rx in _KNOWN_RE:
        if rx.match(t):
            return kind
    return None


def _heuristic_heading(text: str) -> bool:
    t = text.strip()
    if not t or len(t.split()) > 12 or t.endswith((".", "?", "!", ",", ";", ":")) and not _known_kind(t):
        return False
    return bool(_NUMBERED.match(t))


def detect_sections(doc: Document) -> list[Section]:
    use_heuristics = doc.fmt in ("pdf", "txt", "docx")
    # (line index, heading text, kind, level)
    marks: list[tuple[int, str, str, int | None]] = []
    cur_kind = "front"
    cur_level: int | None = None
    in_tex_bib = False
    for idx, ln in enumerate(doc.lines):
        text = ln.text
        if _BIB_BEGIN.search(text):
            marks.append((idx, "References", "references", 0))
            cur_kind, cur_level, in_tex_bib = "references", 0, True
            continue
        if in_tex_bib:
            if _BIB_END.search(text):
                in_tex_bib = False
            continue
        heading = None
        level = ln.heading_level
        if level is not None and text.strip():
            heading = text.strip()
        elif use_heuristics and text.strip():
            known = _known_kind(text)
            special_open = cur_kind in ("references", "acknowledgements", "statements", "appendix")
            if known:
                heading, level = text.strip(), None
            elif not special_open and _heuristic_heading(text):
                heading, level = text.strip(), None
        if heading is None:
            continue
        kind = _known_kind(heading)
        if kind is None:
            if (level is not None and cur_level is not None and cur_kind not in ("body", "front")
                    and level > cur_level):
                kind = cur_kind  # a subsection of references/appendix/... stays in it
            else:
                kind = "body"
        cur_kind, cur_level = kind, level
        marks.append((idx, heading, kind, level))
    sections: list[Section] = []
    lines = doc.lines
    first_idx = marks[0][0] if marks else len(lines)
    pieces: list[tuple[int, int, str, str]] = []  # start idx, end idx exclusive, heading, kind
    if marks:
        if any(l.text.strip() for l in lines[:first_idx]):
            pieces.append((0, first_idx, "", "front"))
    else:
        pieces.append((0, len(lines), "", "body"))
    for i, (idx, heading, kind, _lvl) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(lines)
        pieces.append((idx, end, heading, kind))
    for n, (a, b, heading, kind) in enumerate(pieces, 1):
        body_start = a + 1 if heading else a
        words = sum(len(l.text.split()) for l in lines[body_start:b])
        sections.append(Section(f"s{n}", heading, kind, lines[a].no, lines[b - 1].no, words))
    return sections


def excluded_lines(doc: Document, sections: list[Section]) -> set[int]:
    out: set[int] = set()
    for s in sections:
        if s.kind in EXCLUDED_KINDS:
            out.update(l.no for l in doc.lines if s.start_line <= l.no <= s.end_line)
    in_footnote = False
    for ln in doc.lines:
        if _FOOTNOTE_DEF.match(ln.text):
            out.add(ln.no)
            in_footnote = True
        elif in_footnote and ln.text[:1] in (" ", "\t") and ln.text.strip():
            out.add(ln.no)
        else:
            in_footnote = False
    return out


def default_open_sections(sections: list[Section]) -> list[str]:
    return [s.id for s in sections if s.kind == "body" and _DEFAULT_OPEN.search(s.heading or "")]
