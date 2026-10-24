"""Text extraction into numbered lines for pdf, md, txt, docx, html and tex (plus JATS XML).

Library module; the CLIs call `extract`. Errors are `ExtractError` with a `.code`:
file_not_found, unsupported_format, unreadable_file, no_text_extracted, missing_dependency.
"""
from __future__ import annotations

import hashlib
import io
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_NEWLINE = re.compile(r"\r\n|\n|\r")
EXTENSIONS = {".pdf": "pdf", ".md": "md", ".markdown": "md", ".txt": "txt", ".docx": "docx",
              ".html": "html", ".htm": "html", ".tex": "tex"}


@dataclass
class Line:
    no: int
    text: str
    page: int | None = None
    heading_level: int | None = None
    in_quote: bool = False


@dataclass
class Document:
    lines: list[Line]
    fmt: str
    sha256: str
    warnings: list[str] = field(default_factory=list)


class ExtractError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def split_lines(text: str) -> list[str]:
    """Split on \\r\\n, \\n, \\r only (str.splitlines() also breaks on \\f, \\v, U+2028 and drifts numbers)."""
    parts = _NEWLINE.split(text)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_QUOTE = re.compile(r"^\s{0,3}>\s?")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def _from_md(data: bytes) -> list[Line]:
    out: list[Line] = []
    in_fence = False
    for i, text in enumerate(split_lines(_decode(data)), 1):
        if _FENCE.match(text):
            out.append(Line(i, ""))
            in_fence = not in_fence
        elif in_fence:
            out.append(Line(i, ""))
        elif (m := _HEADING.match(text)):
            out.append(Line(i, m.group(2), heading_level=len(m.group(1))))
        elif _QUOTE.match(text):
            out.append(Line(i, _QUOTE.sub("", text, count=1), in_quote=True))
        else:
            out.append(Line(i, text))
    return out


def _from_txt(data: bytes) -> list[Line]:
    return [Line(i, t.replace("\x0c", " ")) for i, t in enumerate(split_lines(_decode(data)), 1)]


def _from_docx(data: bytes) -> list[Line]:
    try:
        import docx
    except ImportError:
        raise ExtractError("missing_dependency", "python-docx is not installed (pip install -r requirements.txt)")
    try:
        paras = docx.Document(io.BytesIO(data)).paragraphs
    except Exception as exc:  # noqa: BLE001 - any parser failure is an unreadable file
        raise ExtractError("unreadable_file", f"cannot read docx: {type(exc).__name__}") from None
    out = []
    for i, p in enumerate(paras, 1):
        level = None
        style = (p.style.name if p.style is not None else "") or ""
        m = re.match(r"Heading (\d)$", style)
        if m:
            level = int(m.group(1))
        elif style == "Title":
            level = 1
        quote = style in ("Quote", "Intense Quote", "Block Text")
        out.append(Line(i, p.text, heading_level=level, in_quote=quote))
    return out


_BLOCK = {"p", "div", "li", "tr", "br", "section", "article", "ul", "ol", "table", "pre", "dd", "dt",
          "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "header", "footer", "figcaption"}
_SKIP = {"script", "style", "head", "noscript", "template"}


class _HTMLLines(HTMLParser):
    """Each block element becomes one line, followed by a blank line (a paragraph break)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, int | None, bool]] = []
        self.cur: list[str] = []
        self.heading: int | None = None
        self.skip = 0
        self.quote = 0

    def flush(self):
        text = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        self.cur = []
        if text:
            self.rows.append((text, self.heading, self.quote > 0))
            self.rows.append(("", None, False))
        self.heading = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
            return
        if tag in _BLOCK:
            self.flush()
        if tag == "blockquote":
            self.quote += 1
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = int(tag[1])

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in _BLOCK:
            self.flush_keep_heading()
        if tag == "blockquote":
            self.quote = max(0, self.quote - 1)

    def flush_keep_heading(self):
        # the heading level was set at the start tag and must survive until its text is flushed
        heading = self.heading
        text = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        self.cur = []
        if text:
            self.rows.append((text, heading, self.quote > 0))
            self.rows.append(("", None, False))
        self.heading = None

    def handle_data(self, data):
        if not self.skip:
            self.cur.append(data)


def _from_html(data: bytes) -> list[Line]:
    p = _HTMLLines()
    p.feed(_decode(data))
    p.close()
    p.flush()
    return [Line(i, t, heading_level=h, in_quote=q) for i, (t, h, q) in enumerate(p.rows, 1)]


_TEX_SECTION = re.compile(r"\\(chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{(.*)\}")
_TEX_LEVEL = {"chapter": 1, "section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4}
_TEX_QUOTE_BEGIN = re.compile(r"\\begin\{(quote|quotation)\}")
_TEX_QUOTE_END = re.compile(r"\\end\{(quote|quotation)\}")


def _strip_tex_comment(line: str) -> str:
    return re.sub(r"(?<!\\)%.*$", "", line)


def _from_tex(data: bytes) -> list[Line]:
    out = []
    in_quote = False
    for i, raw in enumerate(split_lines(_decode(data)), 1):
        text = _strip_tex_comment(raw).rstrip()
        if _TEX_QUOTE_BEGIN.search(text):
            in_quote = True
            out.append(Line(i, "", in_quote=True))
            continue
        if _TEX_QUOTE_END.search(text):
            in_quote = False
            out.append(Line(i, "", in_quote=True))
            continue
        m = _TEX_SECTION.search(text)
        if m:
            out.append(Line(i, m.group(2).strip(), heading_level=_TEX_LEVEL[m.group(1)]))
        else:
            out.append(Line(i, text, in_quote=in_quote))
    return out


_CID = re.compile(r"\(cid:\d+\)")


def _from_pdf(data: bytes) -> list[Line]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ExtractError("missing_dependency", "pypdf is not installed (pip install -r requirements.txt)")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                raise ExtractError("unreadable_file", "encrypted pdf") from None
        pages = [(p.extract_text() or "") for p in reader.pages]
    except ExtractError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExtractError("unreadable_file", f"cannot read pdf: {type(exc).__name__}") from None
    out: list[Line] = []
    n = 0
    for page_no, text in enumerate(pages, 1):
        text = _CID.sub("", unicodedata.normalize("NFKC", text)).replace("\x0c", " ")
        for t in split_lines(text):
            n += 1
            out.append(Line(n, t, page=page_no))
    return out


_HANDLERS = {"md": _from_md, "txt": _from_txt, "docx": _from_docx, "html": _from_html,
             "tex": _from_tex, "pdf": _from_pdf}


def extract_bytes(data: bytes, fmt: str) -> Document:
    handler = _HANDLERS.get(fmt)
    if handler is None:
        raise ExtractError("unsupported_format", f"unsupported format: {fmt}")
    lines = handler(data)
    if not any(l.text.strip() for l in lines):
        raise ExtractError("no_text_extracted", "no text could be extracted (scanned or empty document)")
    return Document(lines=lines, fmt=fmt, sha256=hashlib.sha256(data).hexdigest(), warnings=[])


def extract(path: Path) -> Document:
    path = Path(path)
    fmt = EXTENSIONS.get(path.suffix.lower())
    if not path.is_file():
        raise ExtractError("file_not_found", f"not a file: {path.name}")
    if fmt is None:
        raise ExtractError("unsupported_format", f"unsupported format: {path.suffix}")
    try:
        data = path.read_bytes()
    except OSError:
        raise ExtractError("unreadable_file", f"cannot read {path.name}") from None
    return extract_bytes(data, fmt)
