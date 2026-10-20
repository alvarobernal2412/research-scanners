"""Evaluate scanner predictions against human-labelled cases (no network)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import ExtractError  # noqa: E402
from scan import build_report  # noqa: E402

CATEGORIES = ("lexical", "structural", "rhetorical", "document", "formatting", "chatbot_residue")


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def evaluate(cases_file: Path, patterns_dir: Path) -> dict:
    cases_file = Path(cases_file)
    cases = json.loads(cases_file.read_text(encoding="utf-8"))
    counts = {c: {"tp": 0, "fp": 0, "fn": 0, "tn": 0} for c in CATEGORIES}
    errors: list[dict] = []
    n = 0
    for case in cases:
        text_path = cases_file.parent / case["text_file"]
        if not text_path.is_file():
            errors.append({"id": case["id"], "error": "file_not_found"})
            continue
        try:
            report = build_report(text_path, Path(patterns_dir))
        except ExtractError as e:
            errors.append({"id": case["id"], "error": e.code})
            continue
        except Exception:
            errors.append({"id": case["id"], "error": "unreadable_file"})
            continue
        if report.get("error"):
            errors.append({"id": case["id"], "error": report["error"]})
            continue
        n += 1
        predicted = {f["category"] for f in report["findings"] if not f["contested"]}
        labels = set(case.get("labels", []))
        for c in CATEGORIES:
            key = ("t" if (c in predicted) == (c in labels) else "f") + ("p" if c in predicted else "n")
            counts[c][key] += 1
    per_category = {}
    for c, v in counts.items():
        per_category[c] = {
            **v,
            "precision": _ratio(v["tp"], v["tp"] + v["fp"]),
            "recall": _ratio(v["tp"], v["tp"] + v["fn"]),
        }
    return {"per_category": per_category, "cases": n, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score scan.py against labelled cases.")
    ap.add_argument("cases")
    ap.add_argument("--patterns-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(evaluate(Path(args.cases), Path(args.patterns_dir)), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
