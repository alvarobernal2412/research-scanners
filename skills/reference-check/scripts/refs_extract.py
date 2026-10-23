"""Extract and parse the reference list of a paper. Local only, no network.

Formats: .bib (best), .tex/.bbl (\\bibitem), .md/.txt, .docx, .pdf.
Structured sources (BibTeX) parse reliably; free-text references are parsed heuristically and fall
back to the raw string, which the lookup uses as a query.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path


class ExtractError(Exception):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail


DOI_RX = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}|\\^`]+)", re.I)
ARXIV_RX = re.compile(r"arxiv[:\s/]*(?:abs/|pdf/)?((?:\d{4}\.\d{4,5})|(?:[a-z\-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?", re.I)
URL_RX = re.compile(r"https?://[^\s<>\"]+", re.I)
YEAR_RX = re.compile(r"\b(1[5-9]\d{2}|20\d{2})[a-z]?\b")
TRAIL = ".,;:)]}>'\""
HEADING_RX = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\d+\.?\s*)?(references|bibliography|works cited|literature cited|referencias|"
    r"reference list|literatur)\s*:?\s*$", re.I)

# ---- LaTeX text cleanup ------------------------------------------------------------------

_ACCENTS = {"`": "\u0300", "'": "\u0301", "^": "\u0302", "~": "\u0303", '"': "\u0308", "=": "\u0304",
            ".": "\u0307", "u": "\u0306", "v": "\u030c", "H": "\u030b", "c": "\u0327", "k": "\u0328"}
_SPECIAL = {r"\ss": "ß", r"\ae": "æ", r"\AE": "Æ", r"\oe": "œ", r"\OE": "Œ", r"\o": "ø", r"\O": "Ø",
            r"\aa": "å", r"\AA": "Å", r"\l": "ł", r"\L": "Ł", r"\i": "i", r"\j": "j"}


def unlatex(s: str) -> str:
    """Turn common LaTeX markup in bibliographic text into plain unicode."""
    if not s:
        return s
    for k, v in _SPECIAL.items():
        s = re.sub(re.escape(k) + r"(?![A-Za-z])\s?", v, s)

    def accent(m):
        mark, ch = m.group(1), m.group(2)
        comb = _ACCENTS.get(mark)
        return unicodedata.normalize("NFC", ch + comb) if comb else ch

    s = re.sub(r"\\([`'^~\"=.])\s*\{?\\?([A-Za-z])\}?", accent, s)
    s = re.sub(r"\\([uvHck])\s*\{([A-Za-z])\}", accent, s)
    s = re.sub(r"\\([uvHck])\s+([A-Za-z])", accent, s)
    s = re.sub(r"\\(?:textit|textbf|emph|textsc|textrm|texttt|mathrm|url|href)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\([&%$#_{}])", r"\1", s)
    s = s.replace("~", " ").replace("---", "\u2014").replace("--", "\u2013")
    s = re.sub(r"[{}]", "", s)
    s = re.sub(r"\\[A-Za-z]+\s*", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---- BibTeX ------------------------------------------------------------------------------

def _read_braced(text: str, i: int) -> tuple[str, int]:
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return text[i + 1:], len(text)


def parse_bibtex(text: str) -> list[dict]:
    entries = []
    for m in re.finditer(r"@(\w+)\s*([{(])", text):
        etype = m.group(1).lower()
        if etype in ("comment", "preamble", "string"):
            continue
        start = m.end() - 1
        body, _ = _read_braced(text, start) if m.group(2) == "{" else (text[m.end():text.find("\n@", m.end())], 0)
        key_m = re.match(r"\s*([^,\s]+)\s*,", body)
        if not key_m:
            continue
        fields: dict[str, str] = {}
        i = key_m.end()
        while i < len(body):
            fm = re.compile(r"\s*([A-Za-z][\w-]*)\s*=\s*").match(body, i)
            if not fm:
                i += 1
                continue
            i = fm.end()
            if i >= len(body):
                break
            if body[i] == "{":
                val, i = _read_braced(body, i)
            elif body[i] == '"':
                j = i + 1
                while j < len(body) and not (body[j] == '"' and body[j - 1] != "\\"):
                    j += 1
                val, i = body[i + 1:j], j + 1
            else:
                vm = re.compile(r"[^,\n]+").match(body, i)
                val, i = (vm.group(0).strip(), vm.end()) if vm else ("", i + 1)
            fields[fm.group(1).lower()] = val
        entries.append({"key": key_m.group(1), "type": etype, "fields": fields})
    return entries


def _split_authors(raw: str) -> list[str]:
    """BibTeX author list to family names."""
    out = []
    for a in re.split(r"\s+and\s+", unlatex(raw)):
        a = a.strip()
        if not a or a.lower() == "others":
            continue
        if "," in a:
            out.append(a.split(",", 1)[0].strip())
        else:
            parts = a.split()
            # keep particles like "van", "de" with the family name
            i = len(parts) - 1
            while i > 0 and parts[i - 1].lower() in ("van", "von", "de", "der", "den", "da", "di", "la", "le", "del"):
                i -= 1
            out.append(" ".join(parts[i:]))
    return out


def bib_entry_to_ref(entry: dict) -> dict:
    f = entry["fields"]
    doi = f.get("doi", "").strip()
    if doi:
        m = DOI_RX.search(doi)
        doi = m.group(1).rstrip(TRAIL) if m else None
    arxiv = None
    if f.get("archiveprefix", "").lower() == "arxiv" and f.get("eprint"):
        arxiv = f["eprint"].strip()
    elif "arxiv" in (f.get("journal", "") + f.get("howpublished", "") + f.get("note", "")).lower():
        m = ARXIV_RX.search(f.get("journal", "") + " " + f.get("note", "") + " " + f.get("howpublished", ""))
        arxiv = m.group(1) if m else None
    venue = f.get("journal") or f.get("booktitle") or f.get("series") or f.get("howpublished") or f.get("publisher")
    url = f.get("url")
    ym = YEAR_RX.search(f.get("year", "") or f.get("date", ""))
    ref = {
        "id": entry["key"],
        "raw": json.dumps({k: unlatex(v) for k, v in f.items()}, ensure_ascii=False),
        "authors": _split_authors(f["author"]) if f.get("author") else [],
        "title": unlatex(f["title"]) if f.get("title") else None,
        "year": int(ym.group(0)[:4]) if ym else None,
        "venue": unlatex(venue) if venue else None,
        "doi": doi or None, "arxiv": arxiv, "url": url, "bibtype": entry["type"],
        "et_al": "others" in f.get("author", "").lower(),
    }
    ref["kind"] = classify(ref)
    return ref


# ---- free text ---------------------------------------------------------------------------

def classify(ref: dict) -> str:
    if ref.get("doi") or ref.get("arxiv"):
        return "identified"
    if ref.get("url") and (ref.get("bibtype") in ("misc", "online", "electronic") or not ref.get("authors")):
        return "web"
    if ref.get("bibtype") in ("misc", "online", "electronic", "manual", "techreport", "standard") and not ref.get("title"):
        return "web"
    raw = ref.get("raw", "")
    if ref.get("url") and (not ref.get("title") or re.search(r"retrieved|accessed|available (at|from|online)|last visited", raw, re.I)):
        return "web"
    return "citation"


_APA_AUTHOR = re.compile(r"([A-Z][\w'’\-]+(?:\s(?:van|von|de|der|den|da|di|la|le|del)\s[A-Z][\w'’\-]+)?),\s*(?:[A-Z]\.\s?-?)+")
_IEEE_AUTHOR = re.compile(r"(?:\b[A-Z]\.\s?-?)+\s+([A-Z][\w'’\-]+)")


def parse_reference_text(raw: str, ref_id: str) -> dict:
    text = re.sub(r"\s+", " ", raw).strip()
    text = re.sub(r"^\s*(?:\[\d+\]|\(\d+\)|\d+[.)])\s*", "", text)
    doi = None
    m = DOI_RX.search(text)
    if m:
        doi = m.group(1).rstrip(TRAIL)
    arxiv = None
    am = ARXIV_RX.search(text)
    if am:
        arxiv = am.group(1)
    urls = [u.rstrip(TRAIL) for u in URL_RX.findall(text)]
    url = next((u for u in urls if "doi.org" not in u), None)
    work = text
    for u in urls:
        work = work.replace(u, " ")
    if doi:
        work = work.replace(doi, " ")
    ym = re.search(r"\((1[5-9]\d{2}|20\d{2})[a-z]?\)", work) or YEAR_RX.search(work)
    year = int(re.search(r"\d{4}", ym.group(0)).group(0)) if ym else None
    title = None
    qm = re.search(r"[\"“”](.{8,}?)[\"“”]", work)
    if qm:
        title = qm.group(1).strip(" ,.")
    else:
        # APA / Chicago: authors (year). Title. Venue
        am2 = re.search(r"\((?:1[5-9]\d{2}|20\d{2})[a-z]?\)\.?\s*(.+?)[.?!]\s", work + " ")
        if am2:
            title = am2.group(1).strip(" ,.")
        else:
            # "Authors. Title. Venue, year." split on sentence boundaries
            parts = re.split(r"(?<=[a-z0-9\)])\.\s+(?=[A-Z0-9])", work)
            if len(parts) >= 3:
                title = parts[1].strip(" ,.")
            elif len(parts) == 2 and len(parts[1]) > 15:
                title = parts[1].split(",")[0].strip(" ,.")
    authors = [m_.group(1) for m_ in _APA_AUTHOR.finditer(work.split(title)[0] if title and title in work else work[:200])]
    if not authors:
        head = work.split(title)[0] if title and title in work else work[:200]
        authors = [m_.group(1) for m_ in _IEEE_AUTHOR.finditer(head)]
    if not authors:
        # "Surname, A. and Surname2, B." free forms; or "Surname et al."
        em = re.match(r"^([A-Z][\w'’\-]+)\s+et al", work)
        if em:
            authors = [em.group(1)]
    venue = None
    if title:
        after = work.split(title, 1)[-1]
        vm = re.search(r"(?:^|[.,]\s*)(?:In:?\s+)?\*?_?([A-Z][^.,\d()]{3,}?)\*?_?(?:,|\.|\s\d|\s\()", after.strip(' ."”“,'))
        if vm:
            venue = vm.group(1).strip(" *_")
    ref = {"id": ref_id, "raw": text, "authors": authors, "title": title, "year": year, "venue": venue,
           "doi": doi, "arxiv": arxiv, "url": url, "bibtype": None,
           "et_al": bool(re.search(r"\bet al\b", work))}
    ref["kind"] = classify(ref)
    return ref


def split_reference_section(text: str) -> list[str]:
    """Return the raw reference strings found after the last 'References' heading."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if HEADING_RX.match(ln):
            start = i + 1
    if start is None:
        raise ExtractError("no_references_found")
    body = [ln.rstrip() for ln in lines[start:]]
    # stop at an appendix heading
    for i, ln in enumerate(body):
        if re.match(r"^\s*(?:#{1,6}\s*)?(appendix|appendices|supplementary|acknowledg)", ln, re.I) and i > 3:
            body = body[:i]
            break
    nonblank = [ln for ln in body if ln.strip()]
    if not nonblank:
        raise ExtractError("no_references_found")
    num_rx = re.compile(r"^\s*(?:\[\d+\]|\(\d+\)|\d{1,3}[.)])\s+\S")
    if sum(1 for ln in nonblank if num_rx.match(ln)) >= max(3, len(nonblank) // 8):
        entries, cur = [], []
        for ln in nonblank:
            if num_rx.match(ln) and cur:
                entries.append(" ".join(cur))
                cur = []
            cur.append(ln.strip())
        if cur:
            entries.append(" ".join(cur))
        return [e for e in entries if len(e) > 20]
    blocks, cur = [], []
    for ln in body:
        if ln.strip():
            cur.append(ln.strip())
        elif cur:
            blocks.append(" ".join(cur))
            cur = []
    if cur:
        blocks.append(" ".join(cur))
    if len(blocks) >= 3:
        return [b for b in blocks if len(b) > 20]
    # one entry per line start that looks like "Surname, I." after a line ending in a period
    entries, cur = [], []
    start_rx = re.compile(r"^[A-Z][\w'’\-]+,\s+(?:[A-Z]\.|[A-Z][a-z]+)")
    for ln in nonblank:
        if cur and start_rx.match(ln.strip()) and cur[-1].rstrip().endswith((".", ")", "]")):
            entries.append(" ".join(cur))
            cur = []
        cur.append(ln.strip())
    if cur:
        entries.append(" ".join(cur))
    return [e for e in entries if len(e) > 20]


def parse_bibitems(text: str) -> list[dict]:
    refs = []
    for m in re.finditer(r"\\bibitem(?:\[[^\]]*\])?\{([^}]*)\}(.*?)(?=\\bibitem|\\end\{thebibliography\}|\Z)", text, re.S):
        raw = unlatex(re.sub(r"\\newblock", " ", m.group(2)))
        if raw:
            refs.append(parse_reference_text(raw, m.group(1)))
    return refs


# ---- files -------------------------------------------------------------------------------

def _document_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".md", ".txt", ".markdown", ".rst"):
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".docx":
        try:
            import docx  # python-docx
        except ModuleNotFoundError:
            raise ExtractError("missing_dependency", "python-docx is needed for .docx files: pip install python-docx")
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs)
    if ext == ".pdf":
        try:
            import pdfplumber
        except ModuleNotFoundError:
            raise ExtractError("missing_dependency", "pdfplumber is needed for .pdf files: pip install pdfplumber")
        with pdfplumber.open(str(path)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        if not text.strip():
            raise ExtractError("no_text_extracted")
        return text
    raise ExtractError("unsupported_format")


def extract_references(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ExtractError("file_not_found")
    ext = path.suffix.lower()
    notes: list[str] = []
    try:
        if ext == ".bib":
            entries = parse_bibtex(path.read_text(encoding="utf-8", errors="replace"))
            refs = [bib_entry_to_ref(e) for e in entries]
            source = "bibtex"
        elif ext in (".tex", ".bbl"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "\\bibitem" in text:
                refs, source = parse_bibitems(text), "bibitem"
            else:
                bibs = re.findall(r"\\bibliography\{([^}]*)\}", text) + re.findall(r"\\addbibresource\{([^}]*)\}", text)
                refs = []
                for name in ",".join(bibs).split(","):
                    name = name.strip()
                    if not name:
                        continue
                    cand = path.parent / (name if name.endswith(".bib") else name + ".bib")
                    if cand.is_file():
                        refs += [bib_entry_to_ref(e) for e in parse_bibtex(cand.read_text(encoding="utf-8", errors="replace"))]
                    else:
                        notes.append(f"bibliography file {name} not found next to the .tex file")
                source = "bibtex"
                if not refs:
                    refs = [parse_reference_text(r, f"ref{i + 1}") for i, r in enumerate(split_reference_section(text))]
                    source = "text"
        else:
            text = _document_text(path)
            if not text.strip():
                raise ExtractError("no_text_extracted")
            raws = split_reference_section(text)
            refs = [parse_reference_text(r, f"ref{i + 1}") for i, r in enumerate(raws)]
            source = "text"
            notes.append("free-text references are parsed heuristically; check entries whose title or authors "
                         "are missing before trusting a 'not_found'")
    except UnicodeError:
        raise ExtractError("unreadable_file")
    if not refs:
        raise ExtractError("no_references_found")
    return {"source": source, "references": refs, "notes": notes}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(extract_references(Path(sys.argv[1])), ensure_ascii=False, indent=2))
    except ExtractError as e:
        print(json.dumps({"error": e.code}))
        sys.exit(2)
