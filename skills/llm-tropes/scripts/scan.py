"""Regex scan engine: find pattern hits in extracted lines."""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import ExtractError, Line, extract  # noqa: E402
from patterns import Pattern, load_patterns  # noqa: E402
from stats import compute_stats  # noqa: E402

DENSE_MIN_CATEGORIES = 3
ENGLISH_MIN_STOPWORD_RATIO = 0.15
PROFILES = ("general", "academic", "technical", "marketing")
DISCLAIMER = (
    "This report shows tell density and evidence only. It does not establish authorship "
    "or whether a human or an AI wrote the text, and such detectors are biased against "
    "non-native writers."
)
_STOPWORDS = frozenset(
    "the of and to in is that it for with as was on are by this be at or from an have "
    "not but which we can has were their".split()
)
_HEADING = re.compile(r"^#{1,6} ")
_WORD = re.compile(r"[\w'’-]+")


@dataclass
class Finding:
    line: int
    text: str
    pattern_id: str
    category: str
    source: str
    weight: float
    contested: bool


def find_hits(lines: list[Line], patterns: list[Pattern]) -> list[Finding]:
    compiled = [(p, re.compile(p.regex, re.IGNORECASE)) for p in patterns if p.regex]
    out: list[Finding] = []
    for ln in lines:
        if ln.in_code or ln.in_quote:
            continue
        for p, rx in compiled:
            for m in rx.finditer(ln.text):
                out.append(Finding(ln.no, m.group(0), p.id, p.category, p.source, p.weight, p.contested))
    return out


def _sections(lines: list[Line], findings: list[Finding]) -> list[dict]:
    if not lines:
        return []
    starts = [(0, "")]
    for idx, ln in enumerate(lines):
        if not ln.in_code and _HEADING.match(ln.text):
            if idx == 0:
                starts = [(0, ln.text.strip())]
            else:
                starts.append((idx, ln.text.strip()))
    out = []
    for n, (idx, heading) in enumerate(starts):
        end_idx = (starts[n + 1][0] if n + 1 < len(starts) else len(lines)) - 1
        lo, hi = lines[idx].no, lines[end_idx].no
        mine = [f for f in findings if lo <= f.line <= hi]
        cats = sorted({f.category for f in mine if not f.contested})
        out.append({
            "heading": heading,
            "start_line": lo,
            "end_line": hi,
            "categories": cats,
            "hit_count": len(mine),
            "dense": len(cats) >= DENSE_MIN_CATEGORIES,
        })
    return out


def _language_note(lines: list[Line]) -> str | None:
    words = [w.lower() for ln in lines if not ln.in_code and not ln.in_quote
             for w in _WORD.findall(ln.text)]
    if len(words) < 20:
        return None
    ratio = sum(w in _STOPWORDS for w in words) / len(words)
    if ratio < ENGLISH_MIN_STOPWORD_RATIO:
        return "The text does not look English, so results may be unreliable."
    return None


def build_report(path: Path, patterns_dir: Path, profile: str = "general") -> dict:
    path = Path(path)
    lines = extract(path)
    patterns = [p for p in load_patterns(Path(patterns_dir))
                if p.profiles is None or profile in p.profiles]
    findings = find_hits(lines, patterns)
    stats = compute_stats(lines)
    words = stats["words"]
    density = sum(f.weight for f in findings) / words * 1000 if words else 0.0
    return {
        "file": str(path),
        "profile": profile,
        "findings": [asdict(f) for f in findings],
        "sections": _sections(lines, findings),
        "stats": stats,
        "density_per_1k": density,
        "language_note": _language_note(lines),
        "disclaimer": DISCLAIMER,
        "error": None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scan a document for LLM tropes (report only).")
    ap.add_argument("file")
    ap.add_argument("--profile", choices=PROFILES, default="general")
    ap.add_argument("--patterns-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = Path(args.file)
    if not path.is_file():
        print(json.dumps({"error": "file_not_found"}))
        return 2
    try:
        report = build_report(path, Path(args.patterns_dir), args.profile)
    except ExtractError as e:
        print(json.dumps({"error": e.code}))
        return 2
    except Exception:  # corrupt or unreadable input
        print(json.dumps({"error": "unreadable_file"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
