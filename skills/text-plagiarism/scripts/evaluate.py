"""Maintainer tool: score the skill on labelled cases (precision, recall, hard gates).

    python -I evaluate.py <cases.json> [--responses DIR] [--live]

Offline by default: every plan/run round is answered from recorded responses and the tool approves its own
plans, so nothing leaves the machine. `--live` uses the real network only for cases marked `public_text: true`
(public identifiers and public CC BY text) and never for anything else.

Case schema: {id, doc_file, tiers: ["strict"] | ["strict", "open"], open_sections, open_backends, include,
              corpus: [paths], expected: [{source_doi, attribution}], known_limitation, coverage_must_mention,
              expect_error, public_text, must_not_send: [text]}.
Scoring: a true positive is a flag/light finding with the expected DOI and attribution; a finding with another
source counts as a false positive and the missed expectation as a false negative; informational findings are
not flags; known limitations are excluded from recall and pass when their coverage items are present.
Gates: privacy (strict requests carry only public identifiers and no 5-word window of the body text; every user text sent was in an approved plan;
text listed in `must_not_send` was never sent), coverage (a case without findings never reports anything but
`no_overlap_found_in_checked_sources` or `insufficient_coverage`), no_verdict (no banned wording in the report).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import open_tier  # noqa: E402
import prepare  # noqa: E402
import report  # noqa: E402
import strict  # noqa: E402
from approval import STRICT_CARRIES, PlannedRequest, content_violation  # noqa: E402
from common import configure_stdout, emit, fail  # noqa: E402
from net import replay_opener  # noqa: E402

BANNED = ("clean", "infring", "probability", "plagiarised", "plagiarized", "guilty")
USER_KEYS = ("passage", "title", "text", "snippet", "url", "text_sent", "note")
ALLOWED_NO_FINDING = ("no_overlap_found_in_checked_sources", "insufficient_coverage")


def _call(main, argv, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv, **kw)
    out = buf.getvalue().strip()
    return code, (json.loads(out) if out else None)


def _drive_strict(wd: Path, opener) -> None:
    for _ in range(12):
        code, plan = _call(strict.main, ["plan", "--workdir", str(wd)])
        if code != 0 or plan.get("done"):
            return
        _call(strict.main, ["run", "--workdir", str(wd), "--plan", plan["plan"], "--sha256", plan["sha256"]],
              opener=opener)


def _drive_open(wd: Path, case: dict, opener) -> None:
    args = ["plan", "--workdir", str(wd), "--sections", ",".join(case.get("open_sections", ["s1"])),
            "--backends", ",".join(case.get("open_backends", ["europepmc"]))]
    if case.get("include"):
        args += ["--include", ",".join(case["include"])]
    code, plan = _call(open_tier.main, args)
    for _ in range(6):
        if code != 0 or plan.get("done"):
            return
        _call(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"], "--sha256", plan["sha256"]],
              opener=opener)
        code, plan = _call(open_tier.main, ["plan", "--workdir", str(wd)])


def _scrub(obj):
    """The report without the user's own words, for the wording check."""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if k not in USER_KEYS}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _log(wd: Path) -> list[dict]:
    p = wd / "requests.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []


def _approved_texts(wd: Path) -> set[str]:
    out: set[str] = set()
    for plan in (wd / "plans").glob("open-*.json") if (wd / "plans").is_dir() else []:
        for r in json.loads(plan.read_text(encoding="utf-8"))["requests"]:
            if r.get("text_sent"):
                out.add(r["text_sent"])
    return out


def content_leaks(wd: Path, log: list[dict]) -> bool:
    """True when a sent request that is not approved user text repeats a 5-word window of the body text."""
    windows = strict.workdir_body_windows(wd)
    for e in log:
        if e.get("carries") == "user_text":
            continue
        req = PlannedRequest(id="", service=e.get("service", ""), method="GET", url=e.get("url") or "",
                             carries=e["carries"], purpose="", text_sent=None)
        if content_violation(req, windows) is not None:
            return True
    return False


def _run_case(case: dict, base: Path, wd: Path, opener) -> dict:
    doc = base / case["doc_file"]
    args = [str(doc), "--workdir", str(wd)]
    for c in case.get("corpus", []):
        args += ["--corpus", str(base / c)]
    code, summary = _call(prepare.main, args)
    result = {"id": case["id"], "error": None, "report": None, "log": [], "approved": set()}
    if code != 0:
        result["error"] = summary.get("error")
        return result
    _drive_strict(wd, opener)
    if "open" in case.get("tiers", ["strict"]):
        _drive_open(wd, case, opener)
    result["report"] = report.build_report(wd)
    result["log"] = _log(wd)
    result["approved"] = _approved_texts(wd)
    return result


def _flags(rep: dict) -> list[dict]:
    return [f for f in rep["findings"] if f["severity"] in ("flag", "light")]


def evaluate(cases_file: Path, responses_dir: Path | None = None, live: bool = False) -> dict:
    cases_file = Path(cases_file)
    base = cases_file.parent
    cases = json.loads(cases_file.read_text(encoding="utf-8"))
    responses_dir = Path(responses_dir) if responses_dir else base.parent / "responses"
    replay = replay_opener(responses_dir)
    tp = fp = fn = tn = 0
    known: list[dict] = []
    errors: list[str] = []
    gates = {"privacy": True, "coverage": True, "no_verdict": True}
    rows = []
    saved = os.environ.get("TEXT_PLAGIARISM_CACHE")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TEXT_PLAGIARISM_CACHE"] = str(Path(tmp) / "cache")
        try:
            for case in cases:
                wd = Path(tmp) / case["id"]
                use_live = live and case.get("public_text", False)
                res = _run_case(case, base, wd, None if use_live else replay)
                row = {"id": case["id"], "live": use_live, "status": None, "tp": 0, "fp": 0, "fn": 0}
                rows.append(row)
                expect_err = case.get("expect_error")
                if res["error"] or expect_err:
                    if res["error"] != expect_err:
                        errors.append(f"{case['id']}: expected error {expect_err!r}, got {res['error']!r}")
                    row["status"] = f"error:{res['error']}"
                    continue
                rep = res["report"]
                row["status"] = rep["coverage"]["status"]
                flags = _flags(rep)
                matched = set()
                for exp in case.get("expected", []):
                    hit = next((i for i, f in enumerate(flags)
                                if i not in matched and f["source"]["doi"] == exp["source_doi"]
                                and f["attribution"] == exp["attribution"]), None)
                    if hit is None:
                        fn += 1
                        row["fn"] += 1
                    else:
                        matched.add(hit)
                        tp += 1
                        row["tp"] += 1
                extra = [f for i, f in enumerate(flags) if i not in matched]
                fp += len(extra)
                row["fp"] += len(extra)
                if extra:
                    errors.append(f"{case['id']}: unexpected finding(s) from " +
                                  ", ".join(str(f["source"]["doi"] or f["source"]["url"]) for f in extra))
                if case.get("known_limitation"):
                    cov = json.dumps(rep["coverage"]).lower()
                    ok = all(m.lower() in cov for m in case.get("coverage_must_mention", []))
                    known.append({"id": case["id"], "coverage_ok": ok})
                    if not ok:
                        errors.append(f"{case['id']}: coverage does not mention {case.get('coverage_must_mention')}")
                    if row["fn"]:       # a known limitation is not counted against recall
                        fn -= row["fn"]
                elif not case.get("expected") and not extra:
                    tn += 1
                # gates
                if content_leaks(wd, res["log"]):
                    gates["privacy"] = False
                for e in res["log"]:
                    if e["tier"] == "strict" and e["carries"] not in STRICT_CARRIES:
                        gates["privacy"] = False
                    if e["carries"] == "user_text" and e.get("text_sent") not in res["approved"]:
                        gates["privacy"] = False
                    blob = urllib.parse.unquote((e.get("url") or "") + " " + (e.get("text_sent") or "")).lower()
                    for banned_text in case.get("must_not_send", []):
                        if banned_text.lower() in blob:
                            gates["privacy"] = False
                    if use_live and not case.get("public_text"):
                        gates["privacy"] = False
                if not rep["findings"] and not rep["informational"] and rep["coverage"]["status"] not in ALLOWED_NO_FINDING:
                    gates["coverage"] = False
                text = json.dumps(_scrub(rep)).lower()
                if any(w in text for w in BANNED):
                    gates["no_verdict"] = False
        finally:
            if saved is None:
                os.environ.pop("TEXT_PLAGIARISM_CACHE", None)
            else:
                os.environ["TEXT_PLAGIARISM_CACHE"] = saved
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": (tp / (tp + fp)) if (tp + fp) else None,
            "recall": (tp / (tp + fn)) if (tp + fn) else None,
            "known_limitations": known, "gates": gates, "cases": rows, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(prog="evaluate.py")
    ap.add_argument("cases")
    ap.add_argument("--responses")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args(argv)
    p = Path(args.cases)
    if not p.is_file():
        return fail("file_not_found", p.name)
    emit(evaluate(p, Path(args.responses) if args.responses else None, args.live))
    return 0


if __name__ == "__main__":
    sys.exit(main())
