import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from evaluate import evaluate  # noqa: E402

DATA = SCRIPTS.parent / "data"
EVAL_PY = SCRIPTS / "evaluate.py"
LEX = "It was a perfect storm of factors.\n"
CLEAN = "The committee reviewed the budget for the coming year and approved the schedule.\n"


def make(tmp_path, cases, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    cf = tmp_path / "cases.json"
    cf.write_text(json.dumps(cases), encoding="utf-8")
    return cf


def test_evaluate_counts_tp_fp_fn_tn(tmp_path):
    cf = make(
        tmp_path,
        [
            {"id": "a", "text_file": "a.md", "labels": ["lexical"]},
            {"id": "b", "text_file": "b.md", "labels": ["lexical"]},
            {"id": "c", "text_file": "c.md", "labels": []},
            {"id": "d", "text_file": "a.md", "labels": []},
        ],
        {"a.md": LEX, "b.md": CLEAN, "c.md": CLEAN},
    )
    r = evaluate(cf, DATA, level="finding")
    lex = r["per_category"]["lexical"]
    assert (lex["tp"], lex["fp"], lex["fn"], lex["tn"]) == (1, 1, 1, 1)
    assert lex["precision"] == 0.5 and lex["recall"] == 0.5
    assert r["cases"] == 4
    assert set(r["per_category"]) == {
        "lexical", "structural", "rhetorical", "document", "formatting", "chatbot_residue"
    }


def test_precision_none_when_no_predictions(tmp_path):
    cf = make(tmp_path, [{"id": "a", "text_file": "a.md", "labels": ["lexical"]}], {"a.md": CLEAN})
    r = evaluate(cf, DATA, level="finding")
    lex = r["per_category"]["lexical"]
    assert lex["precision"] is None
    assert lex["recall"] == 0.0
    assert r["per_category"]["structural"]["recall"] is None


def test_error_cases_reported_separately(tmp_path):
    cf = make(
        tmp_path,
        [
            {"id": "ok", "text_file": "a.md", "labels": ["lexical"]},
            {"id": "gone", "text_file": "missing.md", "labels": ["lexical"]},
        ],
        {"a.md": LEX},
    )
    r = evaluate(cf, DATA, level="finding")
    assert r["cases"] == 1
    assert [e["id"] for e in r["errors"]] == ["gone"]
    assert r["errors"][0]["error"]
    assert r["per_category"]["lexical"]["tp"] == 1


def test_cli_prints_json(tmp_path):
    cf = make(tmp_path, [{"id": "a", "text_file": "a.md", "labels": ["lexical"]}], {"a.md": LEX})
    p = subprocess.run([sys.executable, "-I", str(EVAL_PY), str(cf)], capture_output=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["cases"] == 1


LEXS = "It was a perfect storm of factors."
STRUCTS = "Not a bug. Not a feature. A flaw."
CHATS = "Would you like me to expand on this?"
_PLAIN = (
    "The survey of regional water districts covered forty municipalities over six years. "
    "Response rates varied between the northern and southern counties, and older records were kept on paper. "
    "Households with metered supply reduced consumption after the tariff change, while others showed little response. "
)
CATS = ("lexical", "structural", "chatbot_residue")


def _slop_labels(tmp_path):
    from scan import build_report
    f = tmp_path / "probe.md"
    f.write_text(f"{LEXS} {STRUCTS} {CHATS}\n", encoding="utf-8")
    s = build_report(f, DATA)["sections"][0]
    assert s["dense"], s
    return s["categories"]


def test_section_level_ignores_incidental_hits(tmp_path):
    text = "\n\n".join([_PLAIN] * 6 + ["Moreover, the oldest records were kept on paper."] + [_PLAIN] * 6) + "\n"
    cf = make(tmp_path, [{"id": "a", "text_file": "a.md", "labels": []}], {"a.md": text})
    r = evaluate(cf, DATA)
    assert r["level"] == "section"
    for c, v in r["per_category"].items():
        assert (v["tp"], v["fp"], v["fn"], v["tn"]) == (0, 0, 0, 1), c
    d = r["document_dense"]
    assert (d["tp"], d["fp"], d["fn"], d["tn"]) == (0, 0, 0, 1)
    # finding level would have flagged it
    rf = evaluate(cf, DATA, level="finding")
    assert rf["per_category"]["structural"]["fp"] == 1
    assert rf["level"] == "finding"


def test_section_level_flags_dense_slop(tmp_path):
    labels = _slop_labels(tmp_path)
    assert len(labels) >= 3
    cf = make(tmp_path, [{"id": "a", "text_file": "a.md", "labels": labels}],
              {"a.md": f"{LEXS} {STRUCTS} {CHATS}\n"})
    r = evaluate(cf, DATA)
    for c in labels:
        assert r["per_category"][c]["tp"] == 1, c
    d = r["document_dense"]
    assert (d["tp"], d["fp"], d["fn"], d["tn"]) == (1, 0, 0, 0)
    assert d["precision"] == 1.0 and d["recall"] == 1.0


def test_document_dense_false_negative_and_false_positive(tmp_path):
    cf = make(
        tmp_path,
        [
            {"id": "slop_unlabelled", "text_file": "s.md", "labels": []},
            {"id": "clean_labelled", "text_file": "c.md", "labels": ["lexical"]},
        ],
        {"s.md": f"{LEXS} {STRUCTS} {CHATS}\n", "c.md": CLEAN},
    )
    r = evaluate(cf, DATA)
    d = r["document_dense"]
    assert (d["tp"], d["fp"], d["fn"], d["tn"]) == (0, 1, 1, 0)
    assert d["precision"] == 0.0 and d["recall"] == 0.0
    assert r["per_category"]["lexical"]["fn"] == 1


def test_document_dense_ratios_none_without_denominator(tmp_path):
    cf = make(tmp_path, [{"id": "a", "text_file": "a.md", "labels": []}], {"a.md": CLEAN})
    d = evaluate(cf, DATA)["document_dense"]
    assert d["precision"] is None and d["recall"] is None


def test_cli_level_flag(tmp_path):
    cf = make(tmp_path, [{"id": "a", "text_file": "a.md", "labels": ["lexical"]}], {"a.md": LEX})
    def run(*extra):
        p = subprocess.run([sys.executable, "-I", str(EVAL_PY), str(cf), *extra], capture_output=True, encoding="utf-8")
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)
    assert run()["level"] == "section"
    assert run("--level", "finding")["level"] == "finding"
    assert run("--level", "finding")["per_category"]["lexical"]["tp"] == 1
    assert run("--level", "section")["per_category"]["lexical"]["tp"] == 0
    p = subprocess.run([sys.executable, "-I", str(EVAL_PY), str(cf), "--level", "bogus"], capture_output=True, encoding="utf-8")
    assert p.returncode != 0
