"""Prepare a run: extract the document, detect sections, parse the reference list. No network.

Usage: python -I prepare.py <document> [--workdir W] [--corpus PATH ...]
Writes W/doc.json and prints a summary. Errors are {"error": code} with exit 2.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import configure_stdout, emit, fail  # noqa: E402
from extract import Document, ExtractError, Line, extract  # noqa: E402
from net import cache_root  # noqa: E402
from refs import RefEntry, parse_references  # noqa: E402
from sections import Section, default_open_sections, detect_sections, excluded_lines  # noqa: E402
from textnorm import GLUED_MAX_RATIO, glued_ratio, tokenize  # noqa: E402


def default_workdir(sha256: str) -> Path:
    return cache_root() / "runs" / sha256[:16]


def prepare(path: Path, workdir: Path | None = None) -> tuple[dict, Path]:
    doc = extract(path)
    sections = detect_sections(doc)
    excluded = excluded_lines(doc, sections)
    refs = parse_references(doc, sections)
    body_tokens = [t for t in tokenize(doc.lines) if t.line not in excluded]
    warnings = list(doc.warnings)
    if glued_ratio(body_tokens) > GLUED_MAX_RATIO:
        warnings.append("glued_words")
    if not any(s.kind == "references" for s in sections):
        warnings.append("references_not_found")
    doc.warnings = warnings
    workdir = Path(workdir) if workdir else default_workdir(doc.sha256)
    workdir.mkdir(parents=True, exist_ok=True)
    old = workdir / "doc.json"
    if old.is_file():
        try:
            same = json.loads(old.read_text(encoding="utf-8")).get("document_sha256") == doc.sha256
        except (OSError, ValueError):
            same = False
        if not same:      # another document in this workdir: its plans, state and logs no longer apply
            for name in ("state.json", "open_state.json", "requests.jsonl", "corpus.json"):
                (workdir / name).unlink(missing_ok=True)
            for plan in (workdir / "plans").glob("*.json") if (workdir / "plans").is_dir() else []:
                plan.unlink(missing_ok=True)
    data = {"document_sha256": doc.sha256, "file": Path(path).name, "fmt": doc.fmt, "warnings": warnings,
            "lines": [asdict(l) for l in doc.lines], "sections": [asdict(s) for s in sections],
            "references": [asdict(r) for r in refs]}
    (workdir / "doc.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    defaults = set(default_open_sections(sections))
    summary = {
        "workdir": str(workdir), "document_sha256": doc.sha256,
        "sections": [{"id": s.id, "heading": s.heading, "kind": s.kind, "words": s.words,
                      "default_open": s.id in defaults, "excluded": s.kind in ("references", "acknowledgements", "statements")}
                     for s in sections],
        "references": {"count": len(refs),
                       "with_ids": sum(1 for r in refs if r.doi or r.arxiv),
                       "unpublished": sum(1 for r in refs if r.resolution == "not_sent_unpublished")},
        "warnings": warnings}
    return summary, workdir


def load_doc(workdir: Path) -> tuple[Document, list[Section], list[RefEntry]]:
    data = json.loads((Path(workdir) / "doc.json").read_text(encoding="utf-8"))
    doc = Document(lines=[Line(**l) for l in data["lines"]], fmt=data["fmt"], sha256=data["document_sha256"],
                   warnings=list(data["warnings"]))
    return doc, [Section(**s) for s in data["sections"]], [RefEntry(**r) for r in data["references"]]


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(prog="prepare.py")
    ap.add_argument("document")
    ap.add_argument("--workdir")
    ap.add_argument("--corpus", action="append", default=[])
    args = ap.parse_args(argv)
    try:
        summary, workdir = prepare(Path(args.document), Path(args.workdir) if args.workdir else None)
        if args.corpus:
            import corpus

            summary["corpus"] = corpus.prepare_corpus(workdir, [Path(c) for c in args.corpus])
    except ExtractError as exc:
        return fail(exc.code, str(exc) if exc.code in ("missing_dependency",) else None)
    emit(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
