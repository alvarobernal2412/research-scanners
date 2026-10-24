import json
import subprocess
import sys

import pytest
from approval import (SECRET_ENVS, STRICT_CARRIES, ApprovalError, Gate, PlannedRequest, UnapprovedRequest,
                      build_plan, load_approved_plan, preview, strike, write_plan)
from conftest import SCRIPTS

DOC = "d" * 64


def R(i="", service="crossref", url="https://api.crossref.org/works?rows=2", carries="reference_string", **kw):
    return PlannedRequest(id=i, service=service, method="GET", url=url, carries=carries, purpose="test", **kw)


def U(i="", phrase="exact phrase", **kw):
    return R(i, service="europepmc", url="https://www.ebi.ac.uk/x?query=%22exact%20phrase%22", carries="user_text",
             text_sent=phrase, **kw)


def test_strict_plan_rejects_user_text():
    with pytest.raises(ValueError):
        build_plan("strict", "resolve", DOC, [U()])
    assert "user_text" not in STRICT_CARRIES
    build_plan("open", "search", DOC, [U()])


def test_ids_assigned_in_plan_order_and_duplicates_rejected():
    plan = build_plan("strict", "resolve", DOC, [R(), R(), R()])
    assert [e["id"] for e in plan["requests"]] == ["r001", "r002", "r003"]
    with pytest.raises(ValueError):
        build_plan("strict", "resolve", DOC, [R("a"), R("a")])
    with pytest.raises(ValueError):
        build_plan("strict", "resolve", DOC, [R(carries="whatever")])


def test_text_sent_only_with_user_text():
    with pytest.raises(ValueError):
        build_plan("open", "x", DOC, [R(text_sent="oops")])
    with pytest.raises(ValueError):
        build_plan("open", "x", DOC, [PlannedRequest("", "s", "GET", "https://x", "user_text", "p")])


def test_write_plan_hash_matches_file_bytes(tmp_path):
    import hashlib

    plan = build_plan("strict", "resolve", DOC, [R()])
    p = tmp_path / "plan.json"
    h = write_plan(plan, p)
    assert h == hashlib.sha256(p.read_bytes()).hexdigest()
    assert load_approved_plan(p, h, DOC)["round"] == "resolve"


def test_load_rejects_wrong_hash(tmp_path):
    plan = build_plan("strict", "resolve", DOC, [R()])
    p = tmp_path / "plan.json"
    h = write_plan(plan, p)
    data = bytearray(p.read_bytes())
    data[data.index(b"crossref")] ^= 0x01
    p.write_bytes(bytes(data))
    with pytest.raises(ApprovalError) as e:
        load_approved_plan(p, h, DOC)
    assert e.value.code == "plan_hash_mismatch"


def test_load_missing_invalid_and_other_document(tmp_path):
    with pytest.raises(ApprovalError) as e:
        load_approved_plan(tmp_path / "nope.json", "0" * 64, DOC)
    assert e.value.code == "plan_missing"
    import hashlib

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ApprovalError) as e:
        load_approved_plan(bad, hashlib.sha256(bad.read_bytes()).hexdigest(), DOC)
    assert e.value.code == "plan_invalid"
    p = tmp_path / "plan.json"
    h = write_plan(build_plan("strict", "resolve", DOC, [R()]), p)
    with pytest.raises(ApprovalError) as e:
        load_approved_plan(p, h, "e" * 64)
    assert e.value.code == "plan_document_mismatch"


def test_hand_edited_strict_plan_with_user_text_is_invalid(tmp_path):
    import hashlib

    plan = build_plan("open", "search", DOC, [U()])
    plan["tier"] = "strict"
    p = tmp_path / "plan.json"
    h = write_plan(plan, p)
    with pytest.raises(ApprovalError) as e:
        load_approved_plan(p, h, DOC)
    assert e.value.code == "plan_invalid"


def test_gate_rejects_request_not_in_plan():
    plan = build_plan("strict", "resolve", DOC, [R()])
    gate = Gate(plan)
    gate.check(gate.requests()[0])
    with pytest.raises(UnapprovedRequest):
        gate.check(R("r099"))


def test_gate_rejects_changed_query_string():
    plan = build_plan("open", "search", DOC, [U()])
    gate = Gate(plan)
    req = gate.requests()[0]
    req.url = req.url.replace("exact", "other")
    with pytest.raises(UnapprovedRequest):
        gate.check(req)
    req2 = gate.requests()[0]
    req2.headers = {"X-Extra": "1"}
    with pytest.raises(UnapprovedRequest):
        gate.check(req2)
    req3 = gate.requests()[0]
    req3.text_sent = "something else"
    with pytest.raises(UnapprovedRequest):
        gate.check(req3)


def test_placeholders_only_for_known_secrets():
    ok = R(url="https://api.openalex.org/works?api_key={OPENALEX_API_KEY}", identity="OPENALEX_API_KEY")
    build_plan("strict", "locate", DOC, [ok])
    for bad in (R(url="https://x/?k={SOME_OTHER_KEY}"), R(headers={"Authorization": "Bearer {HOME}"})):
        with pytest.raises(ValueError):
            build_plan("strict", "locate", DOC, [bad])
    assert SECRET_ENVS == {"OPENALEX_API_KEY", "CORE_API_KEY", "BRAVE_API_KEY"}


def test_plan_never_contains_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "super-secret-value-123")
    ok = R(url="https://api.openalex.org/works?api_key={OPENALEX_API_KEY}", identity="OPENALEX_API_KEY")
    p = tmp_path / "plan.json"
    write_plan(build_plan("strict", "locate", DOC, [ok]), p)
    assert b"super-secret-value-123" not in p.read_bytes()
    assert b"{OPENALEX_API_KEY}" in p.read_bytes()
    leaky = R(url="https://api.openalex.org/works?api_key=super-secret-value-123")
    with pytest.raises(ValueError):
        build_plan("strict", "locate", DOC, [leaky])


def test_preview_groups_by_service_and_lists_identity():
    plan = build_plan("open", "search", DOC, [
        U(), U(phrase="second"),
        R(service="brave", url="https://api.search.brave.com/x", carries="user_text", text_sent="third phrase",
          headers={"X-Subscription-Token": "{BRAVE_API_KEY}"}, identity="BRAVE_API_KEY")])
    pv = preview(plan)
    assert pv["services"]["europepmc"]["count"] == 2
    assert pv["services"]["brave"]["identity"] == ["BRAVE_API_KEY"]
    sends = [i["sends"] for i in pv["services"]["europepmc"]["items"]]
    assert sends == ["exact phrase", "second"]


def test_strike_removes_ids_and_changes_hash(tmp_path):
    p = tmp_path / "plan.json"
    h = write_plan(build_plan("open", "search", DOC, [U(), U(phrase="b"), U(phrase="c")]), p)
    h2 = strike(p, ["r2"])
    assert h2 != h
    ids = [e["id"] for e in json.loads(p.read_text(encoding="utf-8"))["requests"]]
    assert ids == ["r001", "r003"]
    with pytest.raises(ApprovalError) as e:
        strike(p, ["r9"])
    assert e.value.code == "unknown_ids"


def test_cli_show_and_strike_json(tmp_path):
    p = tmp_path / "plan.json"
    write_plan(build_plan("open", "search", DOC, [U(), U(phrase="b")]), p)
    script = str(SCRIPTS / "approval.py")
    out = subprocess.run([sys.executable, "-I", script, "show", str(p)], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0
    shown = json.loads(out.stdout)
    assert shown["preview"]["total_requests"] == 2 and len(shown["sha256"]) == 64
    out = subprocess.run([sys.executable, "-I", script, "strike", str(p), "--ids", "r1"], capture_output=True,
                         text=True, encoding="utf-8")
    res = json.loads(out.stdout)
    assert res["removed"] == 1 and res["remaining"] == 1
    out = subprocess.run([sys.executable, "-I", script, "show", str(tmp_path / "none.json")], capture_output=True,
                         text=True, encoding="utf-8")
    assert out.returncode == 2 and json.loads(out.stdout)["error"] == "plan_missing"
