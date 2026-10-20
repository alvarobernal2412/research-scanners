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
    r = evaluate(cf, DATA)
    lex = r["per_category"]["lexical"]
    assert (lex["tp"], lex["fp"], lex["fn"], lex["tn"]) == (1, 1, 1, 1)
    assert lex["precision"] == 0.5 and lex["recall"] == 0.5
    assert r["cases"] == 4
    assert set(r["per_category"]) == {
        "lexical", "structural", "rhetorical", "document", "formatting", "chatbot_residue"
    }


def test_precision_none_when_no_predictions(tmp_path):
    cf = make(tmp_path, [{"id": "a", "text_file": "a.md", "labels": ["lexical"]}], {"a.md": CLEAN})
    r = evaluate(cf, DATA)
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
    r = evaluate(cf, DATA)
    assert r["cases"] == 1
    assert [e["id"] for e in r["errors"]] == ["gone"]
    assert r["errors"][0]["error"]
    assert r["per_category"]["lexical"]["tp"] == 1


def test_cli_prints_json(tmp_path):
    cf = make(tmp_path, [{"id": "a", "text_file": "a.md", "labels": ["lexical"]}], {"a.md": LEX})
    p = subprocess.run([sys.executable, "-I", str(EVAL_PY), str(cf)], capture_output=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["cases"] == 1
