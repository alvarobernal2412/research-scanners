"""Optional user-supplied reference corpus.

Local files are extracted and compared on this machine: they never produce a request, and neither their
paths nor their text appear in any plan. Identifier entries (DOIs, arXiv ids, URLs from a .bib file or a
.txt list) are public identifiers: they join the strict rounds as extra cited-like sources.

Specs accepted by `--corpus`: a document file (pdf, docx, md, txt, html, tex), a directory (recursive),
a .bib file, a .txt list of DOIs / arXiv ids / URLs (one per line, `#` comments), and a `corpus.json`
manifest: [{"file", "doi", "title", "authors", "year", "relation": "related"|"own", "license"}].

    python -I corpus.py status --workdir W
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import configure_stdout, emit, fail  # noqa: E402
from extract import EXTENSIONS, ExtractError, Line, extract_bytes  # noqa: E402
from textnorm import GLUED_MAX_RATIO, glued_ratio, tokenize  # noqa: E402

MAX_CORPUS_FILES = 200
MAX_CORPUS_FILE_BYTES = 50_000_000
MAX_CORPUS_TOTAL_BYTES = 500_000_000
CORPUS_EXTENSIONS = (".pdf", ".docx", ".md", ".txt", ".html", ".htm", ".tex")
STATUSES = ("loaded", "no_text_extracted", "unsupported", "too_large", "over_cap", "unreadable",
            "same_as_document", "pending_fetch", "full_text_not_obtained")

_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")
_ARXIV_NEW = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:\s*)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_BARE_ARXIV = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?")
_URL = re.compile(r"https?://\S+", re.I)
_SKIP_NAMES = {"corpus.json", ".ds_store", "thumbs.db", ".gitkeep"}


@dataclass
class CorpusItem:
    id: str
    kind: str                      # local | identifier
    name: str
    path: str | None
    identifier: str | None
    doi: str | None
    title: str | None
    title_source: str              # manifest | text_guess | metadata
    relation: str
    sha256: str | None
    words: int
    status: str
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    authors: str | None = None
    year: int | None = None
    license: str | None = None


def _clean_doi(raw: str) -> str:
    return raw.rstrip(".,;:)]}>'\"").lower()


# ------------------------------------------------------------- identifier lists and .bib

def parse_identifier_line(line: str) -> dict | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    m = _DOI.search(s)
    if m:
        return {"doi": _clean_doi(m.group()), "identifier": _clean_doi(m.group())}
    a = _ARXIV_NEW.search(s)
    if a:
        return {"arxiv": a.group(1), "identifier": "arXiv:" + a.group(1)}
    if _BARE_ARXIV.fullmatch(s):
        return {"arxiv": s.split("v")[0], "identifier": "arXiv:" + s.split("v")[0]}
    u = _URL.search(s)
    if u:
        return {"url": u.group().rstrip(".,;)"), "identifier": u.group().rstrip(".,;)")}
    return None


def is_identifier_list(text: str) -> bool:
    rows = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    return bool(rows) and len(rows) <= 2000 and all(parse_identifier_line(l) for l in rows)


def parse_bib(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]*)\s*,", text):
        if m.group(1).lower() in ("comment", "string", "preamble"):
            continue
        i, depth, fields = m.end(), 1, {}
        start = i
        while i < len(text) and depth > 0:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        body = text[start:i - 1]
        for f in re.finditer(r"(\w+)\s*=\s*(\{((?:[^{}]|\{[^{}]*\})*)\}|\"([^\"]*)\"|(\d+))", body):
            fields[f.group(1).lower()] = (f.group(3) or f.group(4) or f.group(5) or "").strip()
        rec: dict = {"title": re.sub(r"[{}]", "", fields.get("title", "")) or None,
                     "authors": fields.get("author"), "year": int(fields["year"]) if fields.get("year", "").isdigit() else None}
        if fields.get("doi"):
            rec["doi"] = _clean_doi(fields["doi"].replace("https://doi.org/", ""))
            rec["identifier"] = rec["doi"]
        elif fields.get("eprint") and re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", fields["eprint"]):
            rec["arxiv"] = fields["eprint"].split("v")[0]
            rec["identifier"] = "arXiv:" + rec["arxiv"]
        elif fields.get("url"):
            u = fields["url"].strip()
            if _DOI.search(u):
                rec["doi"] = _clean_doi(_DOI.search(u).group())
                rec["identifier"] = rec["doi"]
            else:
                rec["url"], rec["identifier"] = u, u
        else:
            continue          # an entry with no public identifier cannot be fetched
        out.append(rec)
    return out


# ------------------------------------------------------------- local files

def _strip_running_headers(lines: list[Line]) -> list[Line]:
    pages = sorted({l.page for l in lines if l.page is not None})
    if len(pages) < 3:
        return lines
    seen: Counter = Counter()
    per_page: dict[int, set[str]] = {}
    for l in lines:
        key = re.sub(r"\d+", "#", l.text.strip().lower())
        if len(key) >= 3:
            per_page.setdefault(l.page, set()).add(key)
    for keys in per_page.values():
        seen.update(keys)
    repeated = {k for k, c in seen.items() if c > len(pages) / 2}
    if not repeated:
        return lines
    return [Line(l.no, "" if re.sub(r"\d+", "#", l.text.strip().lower()) in repeated else l.text, l.page,
                 l.heading_level, l.in_quote) for l in lines]


def _guess_title(lines: list[Line]) -> str | None:
    for l in lines[:15]:
        t = l.text.strip()
        if 4 <= len(t) <= 200 and len(t.split()) >= 3 and not re.match(r"^(abstract|page|\d)", t, re.I):
            return t
    return None


def _load_manifest(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


class _Builder:
    def __init__(self, doc_sha256: str, workdir: Path | None):
        self.doc_sha256 = doc_sha256
        self.workdir = workdir
        self.items: list[CorpusItem] = []
        self.files = 0
        self.total_bytes = 0
        self.seen_sha: dict[str, str] = {}
        self.seen_ids: set[str] = set()

    def _id(self) -> str:
        return f"c{len(self.items) + 1}"

    def add_item(self, **kw) -> CorpusItem:
        item = CorpusItem(id=self._id(), **kw)
        self.items.append(item)
        return item

    def add_identifier(self, rec: dict, relation: str = "related") -> None:
        ident = rec["identifier"]
        key = ident.lower()
        if key in self.seen_ids:
            return
        self.seen_ids.add(key)
        self.add_item(kind="identifier", name=ident, path=None, identifier=ident, doi=rec.get("doi"),
                      title=rec.get("title"), title_source="manifest" if rec.get("title") else "metadata",
                      relation=relation, sha256=None, words=0, status="pending_fetch", authors=rec.get("authors"),
                      year=rec.get("year"))

    def add_local(self, path: Path, meta: dict | None = None) -> None:
        meta = meta or {}
        base = dict(kind="local", name=path.name, path=str(path), identifier=None, doi=meta.get("doi"),
                    title=meta.get("title"), title_source="manifest" if meta.get("title") else "text_guess",
                    relation=meta.get("relation") if meta.get("relation") in ("related", "own") else "related",
                    sha256=None, words=0, authors=meta.get("authors"), year=meta.get("year"),
                    license=meta.get("license"))
        if path.suffix.lower() not in CORPUS_EXTENSIONS:
            self.add_item(status="unsupported", reason=f"unsupported file type {path.suffix or '(none)'}", **base)
            return
        self.files += 1
        if self.files > MAX_CORPUS_FILES:
            self.add_item(status="over_cap", reason=f"more than {MAX_CORPUS_FILES} files", **base)
            return
        try:
            size = path.stat().st_size
        except OSError:
            self.add_item(status="unreadable", reason="cannot stat file", **base)
            return
        if size > MAX_CORPUS_FILE_BYTES:
            self.add_item(status="too_large", reason=f"file larger than {MAX_CORPUS_FILE_BYTES} bytes", **base)
            return
        if self.total_bytes + size > MAX_CORPUS_TOTAL_BYTES:
            self.add_item(status="over_cap", reason="corpus size cap reached", **base)
            return
        try:
            data = path.read_bytes()
        except OSError:
            self.add_item(status="unreadable", reason="cannot read file", **base)
            return
        self.total_bytes += size
        sha = hashlib.sha256(data).hexdigest()
        base["sha256"] = sha
        if sha == self.doc_sha256:
            self.add_item(status="same_as_document", reason="the file is the document being checked", **base)
            return
        if sha in self.seen_sha:
            self.add_item(status="unsupported", reason=f"duplicate of {self.seen_sha[sha]}", **base)
            return
        fmt = EXTENSIONS[path.suffix.lower()]
        try:
            doc = extract_bytes(data, fmt)
        except ExtractError as exc:
            status = "no_text_extracted" if exc.code == "no_text_extracted" else "unreadable"
            self.add_item(status=status, reason=exc.code, **base)
            return
        lines = _strip_running_headers(doc.lines)
        warnings: list[str] = []
        tokens = tokenize(lines)
        if glued_ratio(tokens) > GLUED_MAX_RATIO:
            warnings.append("glued_words")
        if not base["doi"]:
            first_page = [l.text for l in lines if l.page in (None, 1)][:150]
            m = _DOI.search(" ".join(first_page))
            if m:
                base["doi"] = _clean_doi(m.group())
                warnings.append("doi_from_text")
        if not base["title"]:
            base["title"] = _guess_title(lines)
        base["words"] = len(tokens)
        item = self.add_item(status="loaded", reason=None, warnings=warnings, **base)
        self.seen_sha[sha] = item.id
        if self.workdir is not None:
            d = self.workdir / "corpus"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{item.id}.json").write_text(json.dumps([asdict(l) for l in lines], ensure_ascii=False),
                                               encoding="utf-8")

    def add_spec(self, spec: Path) -> None:
        if spec.is_dir():
            manifest = {m.get("file"): m for m in _load_manifest(spec / "corpus.json") if m.get("file")}
            for f in sorted(p for p in spec.rglob("*") if p.is_file()):
                if f.name.lower() in _SKIP_NAMES:
                    continue
                rel = f.relative_to(spec).as_posix()
                self._add_file_spec(f, manifest.get(rel))
            for m in _load_manifest(spec / "corpus.json"):
                if not m.get("file") and (m.get("doi") or m.get("url")):
                    self.add_identifier({**m, "identifier": m.get("doi") or m["url"]}, m.get("relation", "related"))
            return
        if not spec.exists():
            self.add_item(kind="local", name=spec.name, path=str(spec), identifier=None, doi=None, title=None,
                          title_source="text_guess", relation="related", sha256=None, words=0,
                          status="unreadable", reason="path does not exist")
            return
        if spec.suffix.lower() == ".json":
            for m in _load_manifest(spec):
                if m.get("file"):
                    self._add_file_spec(spec.parent / m["file"], m)
                elif m.get("doi") or m.get("url"):
                    self.add_identifier({**m, "identifier": m.get("doi") or m["url"]}, m.get("relation", "related"))
            return
        self._add_file_spec(spec, None)

    def _add_file_spec(self, f: Path, meta: dict | None) -> None:
        suffix = f.suffix.lower()
        if suffix == ".bib":
            try:
                text = f.read_bytes().decode("utf-8-sig", errors="replace")
            except OSError:
                self.add_item(kind="local", name=f.name, path=str(f), identifier=None, doi=None, title=None,
                              title_source="text_guess", relation="related", sha256=None, words=0,
                              status="unreadable", reason="cannot read file")
                return
            for rec in parse_bib(text):
                self.add_identifier(rec)
            return
        if suffix == ".txt":
            try:
                text = f.read_bytes().decode("utf-8-sig", errors="replace")
            except OSError:
                text = ""
            if is_identifier_list(text):
                for line in text.splitlines():
                    rec = parse_identifier_line(line)
                    if rec:
                        self.add_identifier(rec)
                return
        self.add_local(f, meta)


def load_corpus(specs: list[Path], doc_sha256: str, workdir: Path | None = None) -> list[CorpusItem]:
    b = _Builder(doc_sha256, Path(workdir) if workdir else None)
    for spec in specs:
        b.add_spec(Path(spec))
    return b.items


# ------------------------------------------------------------- workdir state

def corpus_path(workdir: Path) -> Path:
    return Path(workdir) / "corpus.json"


def read_items(workdir: Path) -> list[CorpusItem]:
    p = corpus_path(workdir)
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return [CorpusItem(**d) for d in data["items"]]


def identifier_items(workdir: Path) -> list[CorpusItem]:
    return [i for i in read_items(workdir) if i.kind == "identifier"]


def item_table(workdir: Path) -> list[dict]:
    """Items with the status of identifier entries derived from the strict rounds' state."""
    from findings import load_state

    sources = {s["key"]: s for s in load_state(workdir).get("sources", [])}
    rows = []
    for item in read_items(workdir):
        row = asdict(item)
        row.pop("path", None)
        if item.kind == "identifier":
            key = (item.doi or "").lower() or (f"arxiv:{item.identifier.split(':')[-1]}" if item.identifier.lower().startswith("arxiv:")
                                               else f"url:{item.identifier}")
            src = sources.get(key)
            if src is None:
                pass
            elif src["status"] == "obtained":
                row.update(status="loaded", reason=None)
            elif src["status"] == "not_obtained":
                row.update(status="full_text_not_obtained", reason="paywalled_provide_pdf", detail=src.get("reason"))
        rows.append(row)
    return rows


def prepare_corpus(workdir: Path, specs: list[Path]) -> dict:
    from prepare import load_doc

    doc, _, _ = load_doc(workdir)
    items = load_corpus(specs, doc.sha256, workdir)
    corpus_path(workdir).write_text(json.dumps({"specs": [s.name for s in specs],
                                                "items": [asdict(i) for i in items]}, ensure_ascii=False),
                                    encoding="utf-8")
    return summary(items)


def summary(items: list[CorpusItem]) -> dict:
    counts = Counter(i.status for i in items)
    return {"counts": dict(counts),
            "local_only": [i.name for i in items if i.kind == "local" and i.status == "loaded"],
            "identifiers": sum(1 for i in items if i.kind == "identifier"),
            "not_loaded": [{"id": i.id, "name": i.name, "status": i.status, "reason": i.reason}
                           for i in items if i.status not in ("loaded", "pending_fetch")]}


def local_lines(workdir: Path, item_id: str) -> list[Line]:
    data = json.loads((Path(workdir) / "corpus" / f"{item_id}.json").read_text(encoding="utf-8"))
    return [Line(**d) for d in data]


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(prog="corpus.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--workdir", required=True)
    args = ap.parse_args(argv)
    wd = Path(args.workdir)
    if not wd.is_dir():
        return fail("workdir_missing", str(wd))
    emit({"items": item_table(wd)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
