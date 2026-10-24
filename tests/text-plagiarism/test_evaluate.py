import json

import evaluate
from conftest import FIXTURES
from helpers import call_main, write_md
from net import replay_opener

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))
A = P["workA"]
CASES = FIXTURES / "cases" / "cases.json"


def doc(body, refs=True):
    r = f"# References\n\n[1] {A['ref_string']}\n" if refs else ""
    return f"# Introduction\n\nWe study logs in this note. {body}\n\n{r}"


def make_cases(tmp_path, cases, docs):
    for name, text in docs.items():
        write_md(tmp_path, name, text)
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(cases), encoding="utf-8")
    return f


def sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def run(tmp_path, cases, docs, live=False):
    return evaluate.evaluate(make_cases(tmp_path, cases, docs), FIXTURES / "responses", live=live)


TP_CASE = {"id": "a", "doc_file": "a.md", "tiers": ["strict"],
           "expected": [{"source_doi": A["doi"], "attribution": "unattributed"}], "public_text": True}
TN_CASE = {"id": "b", "doc_file": "b.md", "tiers": ["strict"], "expected": [], "public_text": True}
DOCS = {"a.md": doc(A["passage"]), "b.md": doc("An original sentence of our own that matches nothing at all in print.")}


def test_counts_on_two_tiny_cases(tmp_path):
    res = run(tmp_path, [TP_CASE, TN_CASE], DOCS)
    assert (res["tp"], res["fp"], res["fn"], res["tn"]) == (1, 0, 0, 1)
    assert res["precision"] == 1.0 and res["recall"] == 1.0 and res["errors"] == []
    assert res["gates"] == {"privacy": True, "coverage": True, "no_verdict": True}
    assert [c["id"] for c in res["cases"]] == ["a", "b"]


def test_wrong_source_is_fp_and_fn(tmp_path):
    wrong = dict(TP_CASE, expected=[{"source_doi": "10.1234/other", "attribution": "unattributed"}])
    res = run(tmp_path, [wrong], DOCS)
    assert (res["tp"], res["fp"], res["fn"]) == (0, 1, 1)
    assert res["precision"] == 0.0 and res["recall"] == 0.0 and res["errors"]
    wrong_attr = dict(TP_CASE, expected=[{"source_doi": A["doi"], "attribution": "cited_not_quoted"}])
    res = run(sub(tmp_path, "x"), [wrong_attr], DOCS)
    assert (res["tp"], res["fp"], res["fn"]) == (0, 1, 1)


def test_known_limitation_excluded_from_recall_but_checked(tmp_path):
    known = {"id": "k", "doc_file": "b.md", "tiers": ["strict"], "known_limitation": True,
             "expected": [{"source_doi": "10.9999/paywalled", "attribution": "unattributed"}],
             "coverage_must_mention": ["Paywalled"]}
    res = run(tmp_path, [known], DOCS)
    assert res["fn"] == 0 and res["recall"] is None and res["tn"] == 0
    assert res["known_limitations"] == [{"id": "k", "coverage_ok": True}] and res["errors"] == []
    bad = dict(known, coverage_must_mention=["a phrase the report never contains"])
    res = run(sub(tmp_path, "y"), [bad], DOCS)
    assert res["known_limitations"] == [{"id": "k", "coverage_ok": False}] and res["errors"]


def test_precision_none_when_no_predictions(tmp_path):
    res = run(tmp_path, [TN_CASE], DOCS)
    assert res["precision"] is None and res["recall"] is None and res["tn"] == 1


def test_expected_error_case_and_mismatch(tmp_path):
    scan = {"id": "s", "doc_file": "empty.md", "tiers": ["strict"], "expect_error": "no_text_extracted"}
    ok = run(tmp_path, [scan], {"empty.md": " \n"})
    assert ok["errors"] == [] and ok["cases"][0]["status"] == "error:no_text_extracted"
    wrong = dict(scan, expect_error="unsupported_format")
    assert run(sub(tmp_path, "z"), [wrong], {"empty.md": " \n"})["errors"]


def test_full_case_set_offline_passes_gates():
    res = evaluate.evaluate(CASES, FIXTURES / "responses")
    assert res["gates"] == {"privacy": True, "coverage": True, "no_verdict": True}
    by = {c["id"]: c for c in res["cases"]}
    for cid in ("P1", "T4", "O1", "C1", "C3"):
        assert by[cid]["tp"] == 1 and by[cid]["fp"] == 0, cid
    for cid in ("T3", "T5", "T6", "T7", "C2"):
        assert by[cid]["tp"] == 0 and by[cid]["fp"] == 0 and by[cid]["fn"] == 0, cid
    assert by["T8"]["status"] == "error:no_text_extracted"
    assert res["errors"] == [] and res["precision"] == 1.0 and res["recall"] == 1.0
    assert res["tp"] == 5 and res["tn"] == 5
    assert res["known_limitations"] == [{"id": "T1", "coverage_ok": True}, {"id": "T2", "coverage_ok": True}]


def test_privacy_gate_trips_when_forbidden_text_is_sent(tmp_path):
    forbidden = dict(TP_CASE, must_not_send=["Robotic process mining"])      # the reference title is sent to Crossref
    res = run(tmp_path, [forbidden], DOCS)
    assert res["gates"]["privacy"] is False


def test_live_flag_skips_non_public_cases(monkeypatch):
    seen = {}
    original = evaluate._run_case
    replay = replay_opener(FIXTURES / "responses")

    def spy(case, base, wd, opener):
        seen[case["id"]] = opener is None            # None would mean the real network
        return original(case, base, wd, replay)      # never touch the network in tests

    monkeypatch.setattr(evaluate, "_run_case", spy)
    evaluate.evaluate(CASES, FIXTURES / "responses", live=True)
    assert {k for k, live in seen.items() if live} == {"P1", "T3", "T4", "T5", "T6", "T7", "T8", "O1"}
    for cid in ("T1", "T2", "C1", "C2", "C3"):
        assert seen[cid] is False
    seen.clear()
    evaluate.evaluate(CASES, FIXTURES / "responses", live=False)
    assert not any(seen.values())


def test_cli(tmp_path):
    code, out = call_main(evaluate.main, [str(CASES)])
    assert code == 0 and out["gates"]["privacy"] is True and out["tp"] == 5
    code, out = call_main(evaluate.main, [str(tmp_path / "none.json")])
    assert code == 2 and out["error"] == "file_not_found"
