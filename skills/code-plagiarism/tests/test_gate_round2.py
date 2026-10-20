"""Fix round 2: gzip DoS, secrets in recordings, HTTP exceptions, containment, path denylist."""
import base64
import copy
import gzip
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import approval  # noqa: E402
import net  # noqa: E402
import replay  # noqa: E402
from approval import ApprovalError, Guard  # noqa: E402
from test_approval import BASE, FU, R1, R2, code, make  # noqa: E402
from test_gate_hardening import Boom, Opener, Resp, guard_for  # noqa: E402

FAKE = "FAKE+key/9a8b=c d"


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


# ---- I1: gzip member flood -----------------------------------------------------------

def test_gzip_member_flood_fails_fast():
    payload = gzip.compress(b"") * 200_000
    assert len(payload) < net.MAX_RESPONSE_BYTES
    t = time.perf_counter()
    with pytest.raises(net.NetError) as e:
        net.gunzip_bounded(payload)
    assert time.perf_counter() - t < 1
    assert e.value.code == "bad_encoding"
    g = guard_for([R1])
    t = time.perf_counter()
    with pytest.raises(net.NetError):
        net.send("GET", R1["url"], g, opener=Opener(Resp(200, {"Content-Encoding": "gzip"}, payload)))
    assert time.perf_counter() - t < 1
    assert g.sent[0]["error"] == "bad_encoding"


def test_gzip_flood_via_recording_opener(tmp_path):
    payload = gzip.compress(b"") * 100_000
    rec = replay.RecordingOpener(Opener(Resp(200, {"Content-Encoding": "gzip"}, payload)), tmp_path)
    t = time.perf_counter()
    with pytest.raises(net.NetError):
        rec.open(urllib.request.Request(R1["url"]))
    assert time.perf_counter() - t < 1


def test_legit_multimember_gzip_still_decodes():
    data = gzip.compress(b"one") + gzip.compress(b"two") + gzip.compress(b"three")
    assert net.gunzip_bounded(data) == b"onetwothree"
    assert net.MAX_GZIP_MEMBERS == 64


def test_gzip_decode_deadline(monkeypatch):
    ticks = iter([0.0, 0.0, 100.0, 200.0, 300.0, 400.0])
    monkeypatch.setattr(net, "_clock", lambda: next(ticks))
    with pytest.raises(net.NetError) as e:
        net.gunzip_bounded(gzip.compress(b"a") * 5)
    assert e.value.code == "decode_timeout"


# ---- I2: secrets in recordings -------------------------------------------------------------

@pytest.mark.parametrize("bad", ["apikey", "api_key", "Key", "access_key", "token", "k"])
def test_secret_token_only_for_param_named_key(bad):
    with pytest.raises(ApprovalError) as e:
        make(requests=[dict(R1, url=BASE + f"/q?a=1&{bad}={{secret:STACKEXCHANGE_KEY}}")], followups=[])
    assert code(e) == "plan_invalid"
    make(requests=[dict(R1, url=BASE + "/q?a=1&key={secret:STACKEXCHANGE_KEY}")], followups=[])


def _record_and_replay(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("STACKEXCHANGE_KEY", FAKE)
    quoted = urllib.parse.quote(FAKE, safe="")
    url = f"{BASE}/q?a=1&apikey={quoted}"
    real = Opener(Resp(200, {}, b"{}"))
    rec = replay.RecordingOpener(real, tmp_path, **kw)
    rec.open(urllib.request.Request(url))
    rep = replay.ReplayOpener(tmp_path, **kw)
    rep.open(urllib.request.Request(url))
    blob = "".join(p.read_text(encoding="utf-8", errors="replace") for p in tmp_path.iterdir())
    blob += json.dumps(rec.requests) + json.dumps(rep.requests)
    return blob


def test_recording_and_replay_redact_secret_by_value(tmp_path, monkeypatch):
    blob = _record_and_replay(tmp_path, monkeypatch)
    assert FAKE not in blob and urllib.parse.quote(FAKE, safe="") not in blob
    assert urllib.parse.quote_plus(FAKE) not in blob


def test_recording_redacts_explicit_values(tmp_path, monkeypatch):
    monkeypatch.delenv("STACKEXCHANGE_KEY", raising=False)
    rec = replay.RecordingOpener(Opener(Resp()), tmp_path, redact=["explicit-secret-123"])
    rec.open(urllib.request.Request(BASE + "/q?token=explicit-secret-123"))
    blob = (tmp_path / "index.json").read_text(encoding="utf-8") + json.dumps(rec.requests)
    assert "explicit-secret-123" not in blob


# ---- M1: HTTPException ------------------------------------------------------------------------

def test_http_exceptions_are_wrapped_and_logged():
    for exc in (http.client.BadStatusLine("x"), http.client.IncompleteRead(b"ab"),
                http.client.LineTooLong("status line"), ConnectionResetError("r")):
        g = guard_for([R1])
        with pytest.raises(net.NetError) as e:
            net.send("GET", R1["url"], g, opener=Opener(exc))
        assert e.value.code == "network_error"
        assert len(g.sent) == 1 and g.sent[0]["status"] is None and g.sent[0]["attempts"] == 1

    class BadRead(Resp):
        def read(self, n=-1):
            raise http.client.IncompleteRead(b"x")

    g = guard_for([R1])
    with pytest.raises(net.NetError) as e:
        net.send("GET", R1["url"], g, opener=Opener(BadRead()))
    assert e.value.code == "network_error" and g.sent


def test_unexpected_exception_still_recorded():
    g = guard_for([R1])
    with pytest.raises(RuntimeError):
        net.send("GET", R1["url"], g, opener=Opener(RuntimeError("boom")))
    assert g.sent and g.sent[0]["error"] == "RuntimeError"


# ---- M2/M3: containment ----------------------------------------------------------------------------

def test_containment_scans_header_values():
    lo = {"map": {"1.js": "src/internal/payroll_module.py"}}
    with pytest.raises(ApprovalError) as e:
        make(local_only=lo, requests=[dict(R1, headers={"Accept": "src/internal/payroll_module.py"})],
             followups=[])
    assert code(e) == "plan_invalid"


def test_containment_substring_inside_token():
    lo = {"map": {"1.js": "src/internal/payroll_module.py"}}
    for body in ("xsrc/internal/payroll_module.pyz", "PRE_src/internal/payroll_module.py_post",
                 "a,b=src/internal/payroll_module.py)"):
        with pytest.raises(ApprovalError) as e:
            make(local_only=lo, requests=[dict(R2, body=body)], followups=[])
        assert code(e) == "plan_invalid", body


def test_hash_exemption_exact_shape_only():
    ok = {"h": {"kind": "hash", "value": "0123456789abcdef0123456789abcdef"}}
    make(local_only=ok, requests=[dict(R2, body="file=0123456789abcdef0123456789abcdef,0,1.js")], followups=[])
    for shape in ({"kind": "hash", "values": ["src/internal/payroll_module.py"]},
                  {"kind": "hash", "value": "src/internal/payroll_module.py", "extra": 1},
                  {"kind": "other", "value": "src/internal/payroll_module.py"},
                  {"kind": "hash", "value": 5, "path": "src/internal/payroll_module.py"}):
        with pytest.raises(ApprovalError):
            make(local_only={"h": shape}, requests=[dict(R2, body="x src/internal/payroll_module.py y")],
                 followups=[])


def test_unusual_character_paths_do_not_block_plans():
    paths = {}
    for i in range(1000):
        paths[f"a{i}.js"] = f"My Project/file {i}.js"
        paths[f"b{i}.js"] = f"src/file({i}).js"
    lo = {"map": paths}
    reqs = [dict(R2, body="file=ab,0,1.js\nline=3\n")]
    plan, secs = timed(lambda: make(local_only=lo, requests=reqs, followups=[]))
    assert secs < 5 and plan["local_only"] == lo
    for leak in ("see My Project/file 777.js here", "x = require('src/file(555).js')",
                 "q: my project/file 12.js"):
        with pytest.raises(ApprovalError) as e:
            make(local_only=lo, requests=[dict(R2, body=leak)], followups=[])
        assert code(e) == "plan_invalid", leak


def test_work_is_bounded_on_adversarial_bodies():
    lo = {"map": {f"{i}.js": f"src/dir{i}/module_{i}.py" for i in range(2000)}}
    _, secs = timed(lambda: make(local_only=lo, requests=[dict(R2, body="a/" * 200_000)], followups=[]))
    assert secs < 5, secs
    b64 = base64.b64encode(os.urandom(300_000)).decode()
    _, secs = timed(lambda: make(local_only=lo, requests=[dict(R2, body=b64)], followups=[]))
    assert secs < 3, secs
    slow = {"map": {f"{i}.js": f"my dir {i}/x ({i}).js" for i in range(200)}}
    _, secs = timed(lambda: make(local_only=slow, requests=[dict(R2, body=b64)], followups=[]))
    assert secs < 4, secs


def test_plan_too_large(monkeypatch):
    monkeypatch.setattr(approval, "MAX_SCAN_BYTES", 1000)
    with pytest.raises(ApprovalError) as e:
        make(local_only={"m": {"1.js": "src/a_long.py"}}, requests=[dict(R2, body="x" * 2000)], followups=[])
    assert e.value.code == "plan_too_large"


def _plan_of(n_req, n_local):
    reqs = [{"id": f"r{i}", "method": "GET", "url": f"{BASE}/search/excerpts?q=tok{i}&site=stackoverflow",
             "body": None, "purpose": "p", "source": None} for i in range(n_req)]
    body = "".join(f"file={i:032x},0,{i}.js\n10={i:08x}\n" for i in range(n_req * 14))
    reqs.append({"id": "w", "method": "POST", "url": "https://api.osskb.org/scan/direct", "body": body,
                 "purpose": "p", "source": None})
    lo = {"map": {f"{i}.js": f"src/dir{i}/module_{i}.py" for i in range(n_local)}}
    return reqs, lo


def test_build_plan_scales_linearly(capsys):
    def build(n):
        reqs, lo = _plan_of(n, 2 * n)
        return timed(lambda: approval.build_plan("scanoss", "fingerprint", "r", reqs, [], [], {}, [], lo))[1]
    build(500)                       # warm up
    small = max(build(2500), 0.05)
    big = build(5000)
    with capsys.disabled():
        print(f"\nSCALING 2500 -> {small:.2f}s, 5000 -> {big:.2f}s")
    assert big < 5 and big < small * 3.5


def test_combined_case_within_budget_report(capsys):
    reqs, lo = _plan_of(10_000, 20_000)
    reqs[-1]["body"] = reqs[-1]["body"][:2_800_000]
    runs = [timed(lambda: approval.build_plan("scanoss", "fingerprint", "r", reqs, [], [], {}, [], lo))[1]
            for _ in range(3)]
    with capsys.disabled():
        print("\nCOMBINED build_plan", [round(r, 2) for r in runs])
    assert max(runs) < 5


# ---- M4: path denylist and misc validation -------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "~/projects/x/a.js", "open('~/secret.txt')", "file:///C:/x/a.js", "file://host/share/a", "see /c/Users/me/a.js",
    "/Volumes/Data/x", "/srv/app/x.py", "/workspace/repo/a.js", "/data/app/x", "/private/var/x", "$HOME/code/a.js",
    "${HOME}/code", "%USERPROFILE%\\code", "%HOMEPATH%\\x", "%APPDATA%\\x", "open('/tmp/x')", "x=/home/me/a.js",
    "D:\\work\\a.js", "D:/work/a.js", "\\\\srv\\share\\a.js",
])
def test_absolute_path_denylist_extended(text):
    with pytest.raises(ApprovalError) as e:
        make(requests=[dict(R2, body=text)], followups=[])
    assert code(e) == "absolute_path_in_plan", text


@pytest.mark.parametrize("text", [
    "import x from './lib/foo.js'", "see https://example.com/home/page and https://a.b/data/x",
    "const re = /search\\/excerpts/", "file=ab,0,1.js", "src/data/file.js", "a ~ b", "100% sure", "price is $5",
    "http://localhost:8080/tmp/x",
])
def test_normal_text_not_flagged(text):
    make(requests=[dict(R2, body=text)], followups=[])


def test_ids_placeholder_not_in_host():
    for tpl in ("https://{ids}.evil.example/x", "https://api.stackexchange.com{ids}/x"):
        with pytest.raises(ApprovalError) as e:
            make(followups=[dict(FU, url_template=tpl)])
        assert code(e) == "plan_invalid"


def test_root_and_version_validation(tmp_path):
    for root in ("..", "a/..", ".."):
        plan = make(root="proj")
        plan["root"] = root
        plan["sha256"] = approval.plan_hash(plan)
        with pytest.raises(ApprovalError) as e:
            Guard(plan)
        assert code(e) == "plan_invalid"
    plan = make()
    plan["version"] = 1.0
    plan["sha256"] = approval.plan_hash(plan)
    with pytest.raises(ApprovalError) as e:
        approval.validate_plan(plan)
    assert code(e) == "plan_invalid"
    p = tmp_path / "v.json"
    text = approval._canonical_text(plan)
    assert '"version": 1.0' in text
    p.write_bytes(text.encode("utf-8"))
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(p, plan["sha256"])
    assert code(e) == "plan_invalid"


@pytest.mark.parametrize("url", [
    BASE + "/q?key=abc", BASE + "/q?a=1&api_key=abc", BASE + "/q?Token=x", BASE + "/q?%6bey=x",
    BASE + "/q?apikey=x", BASE + "/q?access_key=x", BASE + "/q?SECRET=x", BASE + "/q?password=x",
    BASE + "/q?auth=x", BASE + "/q?key=abc&key={secret:STACKEXCHANGE_KEY}",
    BASE + "/q?key={secret:STACKEXCHANGE_KEY}&api_key=abc",
])
def test_literal_secret_looking_params_rejected(url):
    with pytest.raises(ApprovalError) as e:
        make(requests=[dict(R1, url=url)], followups=[])
    assert code(e) == "plan_invalid"


# ---- mutation survivors ----------------------------------------------------------------------------------

def test_guard_validates_plan_structure():
    plan = make()
    plan["requests"].append(copy.deepcopy(plan["requests"][0]))      # duplicate id and request
    plan["sha256"] = approval.plan_hash(plan)
    with pytest.raises(ApprovalError) as e:
        Guard(plan)
    assert code(e) == "plan_invalid"


@pytest.mark.parametrize("url", [
    BASE + "/q?keya{secret:STACKEXCHANGE_KEY}", BASE + "/q?key=x{secret:STACKEXCHANGE_KEY}",
    BASE + "/q?akey={secret:STACKEXCHANGE_KEY}", BASE + "/q?key {secret:STACKEXCHANGE_KEY}",
    BASE + "/q?key:{secret:STACKEXCHANGE_KEY}",
])
def test_secret_token_whole_value_placement(url):
    with pytest.raises(ApprovalError) as e:
        make(requests=[dict(R1, url=url)], followups=[])
    assert code(e) == "plan_invalid"


def test_extra_headers_never_sent(monkeypatch):
    req = dict(R1, headers={"Accept": "application/json"})
    g = guard_for([req])
    op = Opener(Resp())
    net.send("GET", R1["url"], g, headers={"accept": "application/json"}, opener=op)
    items = [(k.lower(), v) for k, v in op.calls[0].header_items()]
    assert sorted(items) == [("accept", "application/json"), ("user-agent", net.USER_AGENT)]
    for extra in ({"Authorization": "Bearer x"}, {"X-Extra": "1"}):
        g = guard_for([req])
        op = Opener(Resp())
        with pytest.raises(ApprovalError):
            net.send("GET", R1["url"], g, headers=extra, opener=op)
        assert op.calls == []


def test_percent_decoded_pass_beyond_find_cap():
    """More than _MAX_FIND_NEEDLES longer needles push the target out of the substring pass."""
    lo = {"map": {f"{i}.js": f"src/very/long/decoy/directory/name/number_{i:03d}/module_{i:03d}.py"
                  for i in range(60)}}
    lo["map"]["t.js"] = "src/pay/ledger.py"
    for body in ("x src%2Fpay%2Fledger.py y", "x%20src%2fpay%2fledger.py"):
        with pytest.raises(ApprovalError) as e:
            make(local_only=lo, requests=[dict(R2, body=body)], followups=[])
        assert code(e) == "plan_invalid", body
    make(local_only=lo, requests=[dict(R2, body="x src%2Fpay%2Fother.py y")], followups=[])
