"""Evaluation harness: scoring rules, replay of the recorded C1-C10 runs, and the live-mode gate."""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"))

import evaluate as ev  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
CASES_JSON = FIXTURES / "cases.json"
CASES = {c["id"]: c for c in json.loads(CASES_JSON.read_text(encoding="utf-8"))}
SO = "https://stackoverflow.com/a/"


def finding(origin, spdx="CC-BY-SA-4.0", status="declared", attribution="absent", match_type="exact", flag=True,
            file="a.js"):
    return {"file": file, "lines": [1, 9], "match_type": match_type, "flag": flag, "flag_reasons": ["unattributed"],
            "attribution": {"status": attribution, "evidence": []},
            "candidates": [{"origin_url": origin, "licenses": [{"spdx": spdx, "status": status}]}]}


def rep(*findings):
    return {"findings": list(findings), "coverage": {"errors": []}}


# ---- origin matching -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("expected, got, ok", [
    (SO + "58437909", "https://stackoverflow.com/a/58437909", True),
    (SO + "58437909", "https://stackoverflow.com/questions/35038857/setting-query-string/58437909#58437909", True),
    (SO + "58437909", "https://stackoverflow.com/revisions/58437909/17", True),
    (SO + "58437909", SO + "58437910", False),
    (SO + "58437909", "https://github.com/o/r", False),
    ("https://github.com/ansible/ansible", "https://github.com/Ansible/ansible/blob/devel/x.py", True),
    ("https://github.com/ansible/ansible", "https://github.com/ansible/ansible.git", True),
    ("https://github.com/ansible/ansible", "https://github.com/ansible/other", False),
    ("https://github.com/git/git", "https://stackoverflow.com/a/1", False),
])
def test_origin_matches(expected, got, ok):
    assert ev.origin_matches(expected, got) is ok


# ---- scoring ---------------------------------------------------------------------------------------------------

def test_tp_requires_origin_license_and_attribution():
    c1 = CASES["C1"]
    assert ev.score_case(c1, rep(finding(SO + "58437909")), "open")["outcome"] == "tp"
    assert ev.score_case(c1, rep(finding(SO + "58437909", spdx="CC-BY-SA-3.0")), "open")["outcome"] == "fp+fn"
    assert ev.score_case(c1, rep(finding(SO + "58437909", attribution="other_reference")), "open")["outcome"] == "fp+fn"
    assert ev.score_case(c1, rep(finding(SO + "1")), "open")["outcome"] == "fp+fn"


def test_match_type_label_is_enforced():
    c10 = CASES["C10"]
    assert ev.score_case(c10, rep(finding(SO + "58437909", match_type="normalised")), "open")["outcome"] == "tp"
    assert ev.score_case(c10, rep(finding(SO + "58437909", match_type="exact")), "open")["outcome"] == "fp+fn"


def test_license_none_label_needs_none_found():
    c9 = CASES["C9"]
    repo = "https://github.com/jsbaan/transformer-from-scratch"
    ok = finding(repo, spdx=None, status="none_found", attribution="other_reference", match_type="near")
    bad = finding(repo, spdx="MIT", attribution="other_reference", match_type="near")
    assert ev.score_case(c9, rep(ok), "fingerprint")["outcome"] == "tp"
    assert ev.score_case(c9, rep(bad), "fingerprint")["outcome"] == "fp+fn"


def test_any_candidate_of_a_flagged_finding_may_be_the_expected_origin():
    f = finding(SO + "1")
    f["candidates"].append(finding(SO + "58437909")["candidates"][0])
    assert ev.score_case(CASES["C1"], rep(f), "open")["outcome"] == "tp"


def test_wrong_source_is_fp_and_fn():
    mit_copy = finding("https://github.com/cloudykangaroo/cloudykangaroo", spdx="MIT", match_type="near")
    assert ev.score_case(CASES["C2"], rep(mit_copy), "fingerprint")["outcome"] == "fp+fn"


def test_flag_false_findings_do_not_count():
    c3 = CASES["C3"]
    informational = finding(SO + "58437909", attribution="present", flag=False)
    assert ev.score_case(c3, rep(informational), "open")["outcome"] == "tn"
    assert ev.score_case(c3, rep(), "open")["outcome"] == "tn"
    assert ev.score_case(c3, rep(finding(SO + "58437909")), "open")["outcome"] == "fp"


def test_missed_reachable_case_is_fn():
    assert ev.score_case(CASES["C2"], rep(), "open")["outcome"] == "fn"
    assert ev.score_case(CASES["C5"], rep(), "fingerprint")["outcome"] == "fn"


def test_unreachable_case_is_limitation_not_fn():
    assert ev.score_case(CASES["C2"], rep(), "strict")["outcome"] == "limitation"
    assert ev.score_case(CASES["C2"], rep(), "fingerprint")["outcome"] == "limitation"
    assert ev.score_case(CASES["C10"], rep(), "fingerprint")["outcome"] == "limitation"
    assert ev.score_case(CASES["C5"], rep(), "all")["outcome"] == "fn"


def test_tally_counts_fp_fn_once_each_and_none_precision():
    per = [{"outcome": o} for o in ("tp", "fp+fn", "fn", "tn", "tn", "limitation")]
    t = ev.tally(per)
    assert (t["tp"], t["fp"], t["fn"], t["tn"], t["limitations"]) == (1, 1, 2, 2, 1)
    assert t["precision"] == 0.5 and t["recall"] == 0.333
    empty = ev.tally([{"outcome": "tn"}, {"outcome": "limitation"}])
    assert empty["precision"] is None and empty["recall"] is None


def test_cases_file_is_complete_and_labelled():
    assert set(CASES) == {"C1", "C1p", "C1b", "C2", "C3", "C4", "C5", "C5b", "C6", "C7", "C8", "C9", "C10"}
    for c in CASES.values():
        assert set(c["labels"]) == {"flag", "origin", "license", "attribution", "match_type"}
        assert set(c["reachable_tiers"]) <= {"strict", "fingerprint", "open"}
        assert c["labels"]["flag"] == bool(c["labels"]["origin"])
        assert (FIXTURES / c["root"]).is_dir()
    assert CASES["C2"]["reachable_tiers"] == ["open"] and CASES["C10"]["labels"]["match_type"] == "normalised"
    assert CASES["C3"]["labels"]["attribution"] == "present"


# ---- replay of the recorded runs ------------------------------------------------------------------------------------

def outcomes(result):
    return {c["id"]: c["outcome"] for c in result["per_case"]}


def test_replay_all_tiers():
    r = ev.evaluate(CASES_JSON, "all")
    out = outcomes(r)
    for cid in ("C1", "C1p", "C1b", "C5", "C5b", "C9", "C10"):
        assert out[cid] == "tp", cid
    for cid in ("C3", "C4", "C6", "C7", "C8"):
        assert out[cid] == "tn", cid
    assert out["C2"] == "fp+fn"          # SCANOSS flags an MIT GitHub copy; the open tier misses the answer
    assert r["errors"] == []
    assert (r["tp"], r["fp"], r["fn"], r["tn"], r["limitations"]) == (7, 1, 1, 5, 0)
    assert r["precision"] == 0.875 and r["recall"] == 0.875


def test_replay_fingerprint_tier():
    r = ev.evaluate(CASES_JSON, "fingerprint")
    assert outcomes(r) == {"C1": "tp", "C1p": "tp", "C2": "fp+fn", "C1b": "limitation", "C3": "tn", "C4": "tn",
                           "C5": "tp", "C5b": "tp", "C6": "tn", "C7": "tn", "C8": "tn", "C9": "tp",
                           "C10": "limitation"}
    assert r["errors"] == []
    assert {k["id"] for k in r["known_limitations"]} == {"C1b", "C10"}


def test_replay_open_tier():
    r = ev.evaluate(CASES_JSON, "open")
    assert outcomes(r) == {"C1": "tp", "C1p": "tp", "C2": "fn", "C1b": "tp", "C3": "tn", "C4": "tn",
                           "C5": "limitation", "C5b": "limitation", "C6": "tn", "C7": "tn", "C8": "tn",
                           "C9": "fp+fn", "C10": "tp"}
    assert r["errors"] == []
    assert r["precision"] == 0.8 and r["recall"] == 0.667


def test_replay_strict_tier_has_no_predictions():
    r = ev.evaluate(CASES_JSON, "strict")
    assert r["tp"] == 0 and r["fp"] == 0 and r["precision"] is None
    assert outcomes(r)["C6"] == "tn" and outcomes(r)["C1"] == "limitation"
    assert r["errors"] == []


def test_replay_c6_vendored_with_own_file_is_not_a_finding():
    r = ev.evaluate(CASES_JSON, "all")
    c6 = next(c for c in r["per_case"] if c["id"] == "C6")
    assert c6["findings"] == []


def test_replay_never_touches_the_network():
    import socket
    with pytest.raises(RuntimeError):
        socket.create_connection(("example.com", 80))
    ev.evaluate(CASES_JSON, "open")        # passes with sockets blocked by conftest


def test_unrecorded_request_is_reported_as_an_error(tmp_path):
    resp = tmp_path / "responses"
    shutil.copytree(FIXTURES / "responses", resp)
    idx = json.loads((resp / "index.json").read_text(encoding="utf-8"))
    victim = next(k for k in idx if "q=URLSearchParams&" in k)
    del idx[victim]
    (resp / "index.json").write_text(json.dumps(idx), encoding="utf-8")
    r = ev.evaluate(CASES_JSON, "open", resp)
    assert any(e["code"] == "network_error" for e in r["errors"])


# ---- live mode: nothing is sent without the combined approval ------------------------------------------------------------

def test_live_without_approve_sends_nothing(tmp_path, capsys):
    plans = tmp_path / "plans"
    rc = ev.main([str(CASES_JSON), "--live", "--plans-dir", str(plans), "--tier", "open"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["sent"] is False
    assert re.fullmatch(r"[0-9a-f]{64}", out["combined_sha256"])
    (plan,) = out["plans"]
    assert plan["name"] == "open" and "PLAN PREVIEW" in plan["preview"] and plan["requests"] > 0
    assert (plans / "open.json").is_file()


def test_live_combined_hash_is_over_sorted_plan_hashes(tmp_path, capsys):
    ev.main([str(CASES_JSON), "--live", "--plans-dir", str(tmp_path / "p"), "--tier", "strict"])
    out = json.loads(capsys.readouterr().out)
    assert out["combined_sha256"] == ev.combined_hash([p["sha256"] for p in out["plans"]])
    assert len(out["plans"]) == len(CASES)


def test_live_with_wrong_approval_is_refused_before_anything_runs(tmp_path, capsys):
    plans = tmp_path / "plans"
    ev.main([str(CASES_JSON), "--live", "--plans-dir", str(plans), "--tier", "open"])
    capsys.readouterr()
    rc = ev.main([str(CASES_JSON), "--live", "--plans-dir", str(plans), "--tier", "open", "--approve", "0" * 64])
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "approval_mismatch"


def test_live_refuses_an_edited_plan(tmp_path, capsys):
    plans = tmp_path / "plans"
    ev.main([str(CASES_JSON), "--live", "--plans-dir", str(plans), "--tier", "open"])
    out = json.loads(capsys.readouterr().out)
    p = plans / "open.json"
    p.write_text(p.read_text(encoding="utf-8").replace("q=URLSearchParams&", "q=URLSearchParamz&"), encoding="utf-8")
    rc = ev.main([str(CASES_JSON), "--live", "--plans-dir", str(plans), "--tier", "open",
                  "--approve", out["combined_sha256"]])
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] in ("plan_hash_mismatch", "plan_invalid")


def test_live_needs_a_plans_dir(capsys):
    assert ev.main([str(CASES_JSON), "--live"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage"


def test_live_fingerprint_without_the_client_is_skipped_not_faked(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("SCANOSS_PY", raising=False)
    rc = ev.main([str(CASES_JSON), "--live", "--plans-dir", str(tmp_path / "p"), "--tier", "fingerprint"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["plans"] == [] and out["skipped_tiers"] == {"fingerprint": "scanoss_not_installed"}


def test_cli_replay_prints_json(capsys):
    assert ev.main([str(CASES_JSON), "--tier", "fingerprint"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tier"] == "fingerprint" and out["tp"] == 5


def test_bad_cases_file_is_an_error(tmp_path, capsys):
    bad = tmp_path / "c.json"
    bad.write_text("{}", encoding="utf-8")
    assert ev.main([str(bad)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "cases_invalid"
