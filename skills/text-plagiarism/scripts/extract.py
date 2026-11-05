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


# A citation command is kept verbatim in the text (attribution and the .bib reader need it); the tokenizer
# ignores it, so it never counts as a word of the passage.
TEX_CITE = re.compile(r"\\(?:[A-Za-z]*cite[A-Za-z]*)\*?(?:\[[^\]]*\]){0,2}\{([^}]*)\}")

# Environments whose content is not prose: dropped from the text (the lines stay, empty, so line numbers hold).
_TEX_SKIP_ENVS = {"tikzpicture", "equation", "align", "alignat", "eqnarray", "gather", "multline", "displaymath",
                  "math", "verbatim", "lstlisting", "minted", "algorithmic", "algorithm", "split"}
_TEX_ENV = re.compile(r"\\(begin|end)\{([A-Za-z]+)\*?\}")
_TEX_PREAMBLE_KEEP = re.compile(r"\s*\\(?:title|author|institute|email|date|subtitle)\b")
_TEX_DROP_WITH_ARG = re.compile(
    r"\\(?:label|ref|eqref|autoref|cref|Cref|pageref|nameref|includegraphics|input|include|usepackage|"
    r"documentclass|bibliographystyle|bibliography|addbibresource|vspace|hspace|vskip|index|url|footnotemark|"
    r"thispagestyle|pagestyle|setlength|newcommand|renewcommand|def|definecolor|graphicspath)\*?"
    r"(?:\[[^\]]*\])*(?:\{[^{}]*\})*")
_TEX_PREFIX_ARG = re.compile(r"\\(?:textcolor|colorbox|href|color)\*?(?:\[[^\]]*\])?\{[^{}]*\}")
_TEX_NAMES = {"ackname": "Acknowledgements", "discintname": "Disclosure of Interests", "refname": "References",
              "abstractname": "Abstract", "contentsname": "Contents"}
_TEX_SYMBOLS = {"&": "&", "%": "%", "_": "_", "#": "#", "$": "$", "{": "", "}": "", "\\": " ", ",": " ", ";": " ",
                ":": " ", "!": "", " ": " ", "ss": "\u00df", "o": "\u00f8", "O": "\u00d8", "ae": "\u00e6",
                "AE": "\u00c6", "l": "\u0142", "L": "\u0141", "i": "i", "j": "j"}
_TEX_ACCENT = {'"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303", "=": "\u0304",
               ".": "\u0307", "v": "\u030c", "c": "\u0327", "u": "\u0306", "H": "\u030b"}
_TEX_ACCENTED = re.compile(r"\\([\"'`^~=.]|[vcuH](?![A-Za-z]))\s*(?:\{\\?([A-Za-z])\}|\\?([A-Za-z]))")
_TEX_MATH = re.compile(r"(?<!\\)\$\$.*?(?<!\\)\$\$|(?<!\\)\$[^$]*(?<!\\)\$|\\\(.*?\\\)|\\\[.*?\\\]")
_TEX_NAME_WORD = re.compile(r"\\([A-Za-z]+)\*?")


def _tex_accent(m: re.Match) -> str:
    return unicodedata.normalize("NFC", (m.group(2) or m.group(3)) + _TEX_ACCENT[m.group(1)])


def _tex_symbol(m: re.Match) -> str:
    return _TEX_SYMBOLS.get(m.group(1), "")


def _tex_plain(seg: str) -> str:
    """One piece of LaTeX (no citation command in it) as the text a reader sees."""
    seg = _TEX_MATH.sub(" ", seg)
    seg = _TEX_DROP_WITH_ARG.sub(" ", seg)
    seg = _TEX_PREFIX_ARG.sub("", seg)
    seg = _TEX_ENV.sub(" ", seg)
    seg = _TEX_ACCENTED.sub(_tex_accent, seg)
    seg = _TEX_NAME_WORD.sub(lambda m: _TEX_NAMES.get(m.group(1), _TEX_SYMBOLS.get(m.group(1), "")), seg)
    seg = re.sub(r"\\([&%_#${}\\,;:! ])", _tex_symbol, seg)
    seg = seg.replace("``", "\u201c").replace("''", "\u201d").replace("---", "\u2014").replace("--", "\u2013")
    seg = seg.replace("~", " ").replace("{", "").replace("}", "")
    return seg


def tex_to_text(line: str) -> str:
    """A line of LaTeX as near-rendered text: formatting commands and their wrappers are removed and the
    words they wrap are kept, math, labels, references and figures are dropped. Citation commands stay."""
    out, pos = [], 0
    for m in TEX_CITE.finditer(line):
        out.append(_tex_plain(line[pos:m.start()]))
        out.append(m.group())
        pos = m.end()
    out.append(_tex_plain(line[pos:]))
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def _from_tex(data: bytes) -> list[Line]:
    out = []
    in_quote = False
    raw_lines = [_strip_tex_comment(r).rstrip() for r in split_lines(_decode(data))]
    in_preamble = any(r.strip().startswith("\\begin{document}") for r in raw_lines)
    skip: list[str] = []          # open non-prose environments
    in_bib = False
    display_math = False
    for i, text in enumerate(raw_lines, 1):
        if in_bib or "\\begin{thebibliography}" in text:
            in_bib = "\\end{thebibliography}" not in text
            out.append(Line(i, text))                 # the reference list is parsed from its source form
            continue
        if in_preamble:
            if text.strip().startswith("\\begin{document}"):
                in_preamble = False
                out.append(Line(i, ""))
            else:
                out.append(Line(i, tex_to_text(text) if _TEX_PREAMBLE_KEEP.match(text) else ""))
            continue
        was_skipping = bool(skip)
        for kind, name in _TEX_ENV.findall(text):
            if name in _TEX_SKIP_ENVS and kind == "begin":
                skip.append(name)
            elif kind == "end" and name in skip:
                skip.remove(name)
        if text.count("$$") % 2 == 1:
            display_math = not display_math
            out.append(Line(i, ""))
            continue
        if was_skipping or skip or display_math:
            out.append(Line(i, ""))
            continue
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
            out.append(Line(i, tex_to_text(m.group(2)), heading_level=_TEX_LEVEL[m.group(1)]))
        else:
            out.append(Line(i, tex_to_text(text), in_quote=in_quote))
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


_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)[A-Za-z][A-Za-z0-9]*;")
_SKIP_JATS = {"xref", "table-wrap", "fig", "ref-list", "back", "supplementary-material", "inline-formula",
              "disp-formula", "label", "object-id"}


def _jats_root(data: bytes):
    import xml.etree.ElementTree as ET

    text = _decode(data)
    text = re.sub(r"<!DOCTYPE[^>\[]*(?:\[[^\]]*\])?[^>]*>", "", text, count=1)
    text = _ENTITY.sub(" ", text)
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        raise ExtractError("unreadable_file", "cannot parse JATS XML") from None


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _jats_text(el) -> str:
    parts: list[str] = []

    def walk(node):
        if _local(node.tag) in _SKIP_JATS:
            if node.tail:
                parts.append(node.tail)
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
        if node.tail:
            parts.append(node.tail)

    if el.text:
        parts.append(el.text)
    for child in el:
        walk(child)
    text = re.sub(r"\s+", " ", "".join(parts))
    text = re.sub(r"\[[\s,;–‐-]*\]", "", text)     # emptied citation brackets
    return re.sub(r"\s+", " ", text).strip()


def _from_jats(data: bytes) -> list[Line]:
    root = _jats_root(data)
    body = next((e for e in root.iter() if _local(e.tag) == "body"), None)
    rows: list[tuple[str, int | None]] = []

    def walk(sec, depth):
        for child in sec:
            tag = _local(child.tag)
            if tag == "title":
                rows.append((_jats_text(child), min(depth, 6)))
            elif tag == "p":
                rows.append((_jats_text(child), None))
                rows.append(("", None))
            elif tag == "sec":
                walk(child, depth + 1)

    if body is not None:
        walk(body, 1)
    return [Line(i, t, heading_level=h) for i, (t, h) in enumerate(rows, 1)]


def jats_info(data: bytes) -> dict:
    """Title, DOI, PMCID and license of a JATS article (license from ali:license_ref or <license>)."""
    root = _jats_root(data)
    info: dict = {"title": None, "doi": None, "pmcid": None, "license": None}
    for e in root.iter():
        tag = _local(e.tag)
        if tag == "article-id":
            kind = e.get("pub-id-type")
            if kind == "doi" and not info["doi"]:
                info["doi"] = (e.text or "").strip().lower()
            elif kind == "pmcid" and not info["pmcid"]:
                info["pmcid"] = (e.text or "").strip()
        elif tag == "article-title" and not info["title"]:
            info["title"] = _jats_text(e)
        elif tag == "license_ref" and not info["license"]:
            info["license"] = (e.text or "").strip()
        elif tag == "license" and not info["license"]:
            link = e.get("{http://www.w3.org/1999/xlink}href")
            info["license"] = link or _jats_text(e) or None
    lic = info["license"] or ""
    m = re.search(r"creativecommons\.org/(?:licenses|publicdomain)/([a-z\-]+)/([\d.]+)", lic)
    if m:
        info["license"] = f"CC {m.group(1).upper()} {m.group(2)}".replace("ZERO", "0")
    return info


_HANDLERS = {"jats": _from_jats, "md": _from_md, "txt": _from_txt, "docx": _from_docx, "html": _from_html,
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
