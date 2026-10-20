"""Text extraction for .md, .txt, .docx and .pdf into numbered lines."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Line:
    no: int
    text: str
    in_code: bool = False
    in_quote: bool = False


class ExtractError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_QUOTE = re.compile(r"^\s{0,3}>")


_NEWLINE = re.compile(r"\r\n|\n|\r")


def _split_lines(text: str) -> list[str]:
    # str.splitlines() also breaks on \f, \v, U+2028 etc., which drifts line numbers.
    parts = _NEWLINE.split(text)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def _read_text(path: Path) -> list[str]:
    return _split_lines(path.read_text(encoding="utf-8-sig"))


def _from_md(path: Path) -> list[Line]:
    out: list[Line] = []
    in_fence = False
    for i, text in enumerate(_read_text(path), 1):
        if _FENCE.match(text):
            out.append(Line(i, text, True, False))
            in_fence = not in_fence
        elif in_fence:
            out.append(Line(i, text, True, False))
        else:
            out.append(Line(i, text, False, bool(_QUOTE.match(text))))
    return out


def _from_txt(path: Path) -> list[Line]:
    return [Line(i, t, False, False) for i, t in enumerate(_read_text(path), 1)]


def _from_docx(path: Path) -> list[Line]:
    import docx

    paras = docx.Document(str(path)).paragraphs
    return [Line(i, p.text, False, False) for i, p in enumerate(paras, 1)]


def _from_pdf(path: Path) -> list[Line]:
    import pdfplumber

    texts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            texts.extend(_split_lines(page.extract_text() or ""))
    lines = [Line(i, t, False, False) for i, t in enumerate(texts, 1)]
    if not any(l.text.strip() for l in lines):
        raise ExtractError("no_text_extracted", f"no text extracted from {path.name}")
    return lines


_HANDLERS = {
    ".md": _from_md,
    ".markdown": _from_md,
    ".txt": _from_txt,
    ".docx": _from_docx,
    ".pdf": _from_pdf,
}


def extract(path: Path) -> list[Line]:
    path = Path(path)
    handler = _HANDLERS.get(path.suffix.lower())
    if handler is None:
        raise ExtractError("unsupported_format", f"unsupported format: {path.suffix}")
    return handler(path)
