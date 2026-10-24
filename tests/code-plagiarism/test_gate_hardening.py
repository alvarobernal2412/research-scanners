"""Fix round 1: adversarial hardening of the approval gate and guarded transport."""
import gzip
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import approval  # noqa: E402
import net  # noqa: E402
import replay  # noqa: E402
from approval import ApprovalError, Guard  # noqa: E402
from test_approval import BASE, FU, R1, R2, R3, code, make, written  # noqa: E402

SE_URL = BASE + "/search/excerpts?q=alpha&site=stackoverflow&key={secret:STACKEXCHANGE_KEY}"
SE_REQ = dict(R1, id="k1", url=SE_URL)
FAKE = "FAKEKEY-9a8b7c6d"


class Resp:
    def __init__(self, status=200, headers=None, body=b"{}"):
        self.status, self.headers, self._b = status, headers or {}, body

    def read(self, n=-1):
        return self._b if n is None or n < 0 else self._b[:n]

    def close(self):
        pass

    def getcode(self):
        return self.status


class Opener:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def open(self, req, timeout=None):
        self.calls.append(req)
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


class Boom:
    def open(self, *a, **k):
        raise AssertionError("opener must not be called")


def guard_for(requests, followups=None):
    return Guard(make(requests=requests, followups=followups if followups is not None else []))


# ---- 1. time budgets (20k local_only, 2.8 MB body) ---------------------------------

def _wfp_body(target=2_800_000):
    lines, size, i = [], 0, 0
    while size < target:
        line = f"file={i:032x},0,{i}.js\n10={i % 99999999:08x}\n"
        lines.append(line)
        size += len(line)
        i += 1
    return "".join(lines)


def test_time_budgets_realistic_plan(capsys):
    reqs = [{"id": f"r{i}", "method": "GET",
             "url": f"{BASE}/search/excerpts?q=tok{i}&site=stackoverflow",
             "body": None, "purpose": f"p{i}", "source": {"rel": f"src/f{i}.js", "start": 1, "end": 9}}
            for i in range(10_000)]
    reqs.append({"id": "wfp", "method": "POST", "url": "https://api.osskb.org/scan/direct",
                 "body": _wfp_body(), "purpose": "fingerprint", "source": None})
    local = {"map": {f"{i}.js": f"src/dir{i}/module_{i}.py" for i in range(20_000)}}
    timings = {}

    def timed(name, fn):
        t = time.perf_counter()
        out = fn()
        timings[name] = time.perf_counter() - t
        return out

    plan = timed("build_plan", lambda: approval.build_plan(
        "scanoss", "fingerprint", "repo", reqs, [FU], ["d"], {"requests": len(reqs)}, [], local))
    g = timed("Guard()", lambda: Guard(plan))

    def checks():
        for r in reqs[:10_000]:
            g.check("GET", r["url"], None)
    timed("check x10k", checks)
    timed("preview", lambda: approval.render_preview(plan))
    new = timed("strike", lambda: approval.strike(plan, {f"r{i}" for i in range(0, 10_000, 2)}))
    assert len(new["requests"]) == 5001
    with capsys.disabled():
        print("\nTIMINGS", {k: round(v, 2) for k, v in timings.items()})
    for name, secs in timings.items():
        assert secs < 5, (name, secs)   # generous: scaling is asserted in test_gate_round2


def test_containment_not_rerun_by_guard_or_load(tmp_path, monkeypatch):
    plan = make()
    p, _ = written(tmp_path, plan)
    monkeypatch.setattr(approval, "_check_containment",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    Guard(plan)
    approval.load_plan(p, plan["sha256"])


# ---- 10. containment ---------------------------------------------------------------

def test_containment_catches_basename_key_and_percent_encoded():
    lo = {"map": {"1.js": "src/internal/payroll_module.py"}}
    for url in (BASE + "/q?x=payroll_module.py", BASE + "/q?x=src%2Finternal%2Fpayroll_module.py",
                BASE + "/q?x=src/internal/payroll_module.py"):
        with pytest.raises(ApprovalError) as e:
            make(local_only=lo, requests=[dict(R1, url=url)])
        assert code(e) == "plan_invalid"
    with pytest.raises(ApprovalError):
        make(local_only={"src/billing/ledger_core.py": "id7"},
             requests=[dict(R2, body="see src/billing/ledger_core.py")])
    with pytest.raises(ApprovalError):
        make(local_only=lo, requests=[dict(R2, body="x\nsrc\\internal\\payroll_module.py\ny")])


def test_containment_hash_kind_exempt():
    lo = {"h0": {"kind": "hash", "value": "0123456789abcdef0123456789abcdef"}, "map": {"1.js": "src/a.js"}}
    plan = make(local_only=lo, requests=[dict(R2, body="file=0123456789abcdef0123456789abcdef,0,1.js\n")],
                followups=[])
    assert plan["local_only"] == lo


# ---- 2. absolute paths anywhere ------------------------------------------------------

@pytest.mark.parametrize("mut", [
    lambda r, p: r.update(body="file=x,0,C:\\Users\\alvaro\\proj\\a.js"),
    lambda r, p: r.update(body="file=x,0,C:/Users/alvaro/proj/a.js"),
    lambda r, p: r.update(url=BASE + "/q?x=/home/alvaro/secret.js"),
    lambda r, p: r.update(purpose="from /Users/alvaro/code"),
    lambda r, p: r.update(body="x \\\\server\\share\\a.js"),
    lambda r, p: r.update(body="path='/tmp/build/a.js'"),
    lambda r, p: p["disclosure"].append("scanned C:\\work\\repo"),
    lambda r, p: p["cost"].update(note="/var/lib/x"),
    lambda r, p: r.update(headers={"Accept": "/etc/passwd"}),
])
def test_absolute_paths_rejected_everywhere(mut):
    reqs = [dict(R2, id="a", method="POST")]
    plan_kw = {"disclosure": ["d"], "cost": {"requests": 1}}

    class P(dict):
        pass
    holder = P(plan_kw)
    mut(reqs[0], holder)
    with pytest.raises(ApprovalError) as e:
        make(requests=reqs, followups=[], disclosure=holder["disclosure"], cost=holder["cost"])
    assert code(e) == "absolute_path_in_plan"


def test_relative_and_url_paths_are_fine():
    body = "import foo from './lib/foo.js'\nconst re = /search\\/excerpts/\nfile=ab,0,1.js\n"
    plan = make(requests=[dict(R1, url=BASE + "/search/excerpts?q=a/b&site=x"),
                          dict(R2, body=body, purpose="uses https://example.com/home/page")], followups=[])
    assert plan["requests"][1]["body"] == body


# ---- 3. response caps -----------------------------------------------------------------

def test_gzip_bomb_is_capped():
    bomb = gzip.compress(b"\0" * (net.MAX_RESPONSE_BYTES + 5_000_000), 9)
    assert len(bomb) < 100_000
    g = guard_for([R1])
    with pytest.raises(net.NetError) as e:
        net.send("GET", R1["url"], g, opener=Opener(Resp(200, {"Content-Encoding": "gzip"}, bomb)))
    assert e.value.code == "response_too_large"


def test_oversize_plain_body_and_memoryerror():
    g = guard_for([R1])
    with pytest.raises(net.NetError) as e:
        net.send("GET", R1["url"], g, opener=Opener(Resp(200, {}, b"x" * (net.MAX_RESPONSE_BYTES + 10))))
    assert e.value.code == "response_too_large"

    class Mem(Resp):
        def read(self, n=-1):
            raise MemoryError

    g = guard_for([R1])
    with pytest.raises(net.NetError) as e:
        net.send("GET", R1["url"], g, opener=Opener(Mem()))
    assert e.value.code == "response_too_large"


def test_gzip_multimember_and_truncated():
    g = guard_for([R1])
    r = net.send("GET", R1["url"], g, opener=Opener(
        Resp(200, {"Content-Encoding": "gzip"}, gzip.compress(b"ab") + gzip.compress(b"cd"))))
    assert r.body == b"abcd"
    g = guard_for([R1])
    with pytest.raises(net.NetError) as e:
        net.send("GET", R1["url"], g, opener=Opener(
            Resp(200, {"Content-Encoding": "gzip"}, gzip.compress(b"x" * 1000)[:-8])))
    assert e.value.code == "network_error"


def test_recording_opener_caps_gunzip(tmp_path):
    bomb = gzip.compress(b"\0" * (net.MAX_RESPONSE_BYTES + 1_000_000), 9)
    rec = replay.RecordingOpener(Opener(Resp(200, {"Content-Encoding": "gzip"}, bomb)), tmp_path)
    with pytest.raises(net.NetError) as e:
        rec.open(urllib.request.Request(R1["url"]))
    assert e.value.code == "response_too_large"


# ---- 4/6. str subclasses and control characters ---------------------------------------

class EvilStr(str):
    def __eq__(self, other):
        return True

    def __hash__(self):
        return hash(R1["url"])


def test_str_subclass_rejected():
    g = guard_for([R1])
    for m, u in ((EvilStr("GET"), R1["url"]), ("GET", EvilStr("https://evil.example/"))):
        with pytest.raises(ApprovalError) as e:
            net.send(m, u, g, opener=Boom())
        assert code(e) == "request_not_in_plan"
    g = guard_for([dict(R1, headers={"Accept": "a/b"})])

    class S(str):
        pass
    for h in ({S("Accept"): "a/b"}, {"Accept": S("a/b")}):
        with pytest.raises(ApprovalError):
            g.check("GET", R1["url"], None, headers=h)


def test_trailing_newline_and_controls():
    g = guard_for([R1])
    for suffix in ("\n", "\r\n", "\x0b", "\t", "\x7f"):
        with pytest.raises(ApprovalError) as e:
            g.check("GET", R1["url"] + suffix, None)
        assert code(e) == "request_not_in_plan"
    with pytest.raises(ApprovalError) as e:
        make(requests=[dict(R1, url=R1["url"] + "\n")])
    assert code(e) == "plan_invalid"
    g = guard_for([dict(R1, headers={"Accept": "a/b"})])
    for h in ({"Accept": "a/b\n"}, {"Accept\n": "a/b"}, {"Accept": "a/b\x7f"}):
        with pytest.raises(ApprovalError):
            g.check("GET", R1["url"], None, headers=h)


# ---- 5. secret allowlist ----------------------------------------------------------------

def test_secret_env_allowlist(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AKIAFAKE")
    with pytest.raises(ApprovalError) as e:
        make(requests=[dict(R1, headers={"X-Key": {"secret_from_env": "AWS_SECRET_ACCESS_KEY"}})])
    assert code(e) == "plan_invalid"
    with pytest.raises(net.NetError) as e:
        net._secret_value("AWS_SECRET_ACCESS_KEY")
    assert e.value.code == "secret_invalid" and "AKIAFAKE" not in str(e.value)
    plan = make(requests=[dict(R1, headers={"X-Key": {"secret_from_env": "STACKEXCHANGE_KEY"}})], followups=[])
    assert plan["requests"][0]["headers"]["X-Key"] == {"secret_from_env": "STACKEXCHANGE_KEY"}


# ---- 7. repeats / followup limits ----------------------------------------------------------

def test_identical_request_twice_rejected_but_retry_ok():
    g = guard_for([R1, R2])
    g.check("GET", R1["url"], None)
    with pytest.raises(ApprovalError) as e:
        g.check("GET", R1["url"], None)
    assert code(e) == "request_not_in_plan"
    g = guard_for([R1])
    r = net.send("GET", R1["url"], g, opener=Opener(Resp(429), Resp(503), Resp(200)), sleep=lambda s: None)
    assert r.status == 200
    with pytest.raises(ApprovalError):
        net.send("GET", R1["url"], g, opener=Boom())


def test_followup_max_requests_and_id_bounds():
    fu = dict(FU, max_requests=2)
    g = Guard(make(followups=[fu]))
    g.allow_ids([1, 2, 3])
    base = BASE + "/answers/{}?site=stackoverflow&filter=x"
    g.check("GET", base.format("1"), None)
    g.check("GET", base.format("2"), None)
    with pytest.raises(ApprovalError) as e:
        g.check("GET", base.format("3"), None)
    assert code(e) == "followup_not_allowed"
    for bad in (0, 2 ** 63, 10 ** 40):
        with pytest.raises(ApprovalError) as e:
            g.allow_ids([bad])
        assert code(e) == "followup_not_allowed"
    g.allow_ids([2 ** 63 - 1])
    huge = base.format("9" * 5000)
    with pytest.raises(ApprovalError) as e:
        g.check("GET", huge, None)
    assert code(e) == "followup_not_allowed"
    with pytest.raises(ApprovalError):
        make(followups=[{k: v for k, v in FU.items() if k != "max_requests"}])
    with pytest.raises(ApprovalError):
        make(followups=[dict(FU, max_requests=0)])


# ---- 8. sent log records outcome ---------------------------------------------------------------

def test_sent_log_records_outcome_and_not_on_secret_failure(monkeypatch):
    monkeypatch.delenv("STACKEXCHANGE_KEY", raising=False)
    req = dict(R1, headers={"X-Key": {"secret_from_env": "STACKEXCHANGE_KEY"}})
    g = guard_for([req])
    with pytest.raises(net.NetError) as e:
        net.send("GET", R1["url"], g, opener=Boom())
    assert e.value.code == "secret_missing" and g.sent == []
    monkeypatch.setenv("STACKEXCHANGE_KEY", FAKE)
    net.send("GET", R1["url"], g, opener=Opener(Resp(429), Resp(200)), sleep=lambda s: None)
    assert len(g.sent) == 1
    assert g.sent[0]["status"] == 200 and g.sent[0]["attempts"] == 2
    assert FAKE not in json.dumps(g.sent)
    g = guard_for([R1])
    with pytest.raises(net.NetError):
        net.send("GET", R1["url"], g, opener=Opener(urllib.error.URLError("x")))
    assert g.sent[0]["status"] is None and g.sent[0]["error"] == "network_error"


# ---- Stack Exchange key token --------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    BASE + "/q?key={secret:STACKEXCHANGE_KEY}{secret:STACKEXCHANGE_KEY}",
    BASE + "/q?a={secret:STACKEXCHANGE_KEY}&key={secret:STACKEXCHANGE_KEY}",
    BASE + "/q?key=a{secret:STACKEXCHANGE_KEY}",
    BASE + "/q?key={secret:STACKEXCHANGE_KEY}b",
    BASE + "/q?{secret:STACKEXCHANGE_KEY}",
    BASE + "/{secret:STACKEXCHANGE_KEY}/q?a=1",
    "https://{secret:STACKEXCHANGE_KEY}.example.com/q?a=1",
    BASE + "/q?key={secret:AWS_SECRET_ACCESS_KEY}",
    BASE + "/q?key={secret:}",
    BASE + "/q?key={secret:lower}",
])
def test_secret_token_placement_rules(bad):
    with pytest.raises(ApprovalError) as e:
        make(requests=[dict(R1, url=bad)], followups=[])
    assert code(e) == "plan_invalid"


def test_secret_token_not_in_body_or_header():
    with pytest.raises(ApprovalError):
        make(requests=[dict(R2, body="a {secret:STACKEXCHANGE_KEY} b")], followups=[])
    with pytest.raises(ApprovalError):
        make(requests=[dict(R1, headers={"Accept": "{secret:STACKEXCHANGE_KEY}"})], followups=[])
    ok = make(requests=[dict(R2, body="d = {secret: 1}")], followups=[])      # not the token shape
    assert ok["requests"][0]["body"] == "d = {secret: 1}"


def test_secret_token_in_followup_template():
    fu = dict(FU, url_template=BASE + "/answers/{ids}?site=x&key={secret:STACKEXCHANGE_KEY}")
    plan = make(followups=[fu])
    g = Guard(plan)
    g.allow_ids([5])
    g.check("GET", BASE + "/answers/5?site=x&key={secret:STACKEXCHANGE_KEY}", None)


def test_secret_value_in_wire_only(tmp_path, monkeypatch):
    monkeypatch.setenv("STACKEXCHANGE_KEY", FAKE)
    plan = make(requests=[SE_REQ], followups=[])
    p, _ = written(tmp_path, plan)
    preview = approval.render_preview(plan)
    assert FAKE not in p.read_text(encoding="utf-8") and FAKE not in preview
    assert "key=<secret from STACKEXCHANGE_KEY; not shown>" in preview
    assert "keyed" in preview.lower()
    g = Guard(plan)
    op = Opener(Resp(200, {}, b"{}"))
    net.send("GET", SE_URL, g, opener=op)
    assert op.calls[0].full_url == SE_URL.replace("{secret:STACKEXCHANGE_KEY}", FAKE)
    assert FAKE not in json.dumps(g.sent) and "{secret:STACKEXCHANGE_KEY}" in json.dumps(g.sent)
    # percent-encoding of awkward values
    monkeypatch.setenv("STACKEXCHANGE_KEY", "a&b=c d")
    g = Guard(plan)
    op = Opener(Resp())
    net.send("GET", SE_URL, g, opener=op)
    assert op.calls[0].full_url.endswith("key=a%26b%3Dc%20d")
    # error paths never carry the value
    monkeypatch.setenv("STACKEXCHANGE_KEY", FAKE)
    g = Guard(plan)
    with pytest.raises(net.NetError) as e:
        net.send("GET", SE_URL, g, opener=Opener(urllib.error.URLError(FAKE)))
    assert FAKE not in str(e.value) and FAKE not in repr(e.value)
    g = Guard(plan)
    with pytest.raises(ApprovalError) as e:
        net.send("GET", SE_URL.replace("{secret:STACKEXCHANGE_KEY}", FAKE), g, opener=Boom())
    assert FAKE not in str(e.value) and FAKE not in repr(e.value)
    monkeypatch.delenv("STACKEXCHANGE_KEY")
    g = Guard(plan)
    with pytest.raises(net.NetError) as e:
        net.send("GET", SE_URL, g, opener=Boom())
    assert e.value.code == "secret_missing" and g.sent == []


def test_exception_text_truncates_query():
    g = guard_for([R1])
    with pytest.raises(ApprovalError) as e:
        g.check("GET", BASE + "/search/excerpts?q=SUPERSECRETIDENT&site=x", None)
    assert "SUPERSECRETIDENT" not in str(e.value)


def test_replay_strips_keys_case_insensitive(tmp_path):
    for url in (BASE + "/x?q=1&KEY=S", BASE + "/x?access_key=S&q=1", BASE + "/x?q=1&Key=S&z=2"):
        assert "S" not in replay.request_key("GET", url, None).split("?")[-1].replace("q=1", "").replace("z=2", "")
    assert replay.request_key("GET", BASE + "/x?q=1&Key=S&z=2", None) == f"GET {BASE}/x?q=1&z=2"
    assert replay.request_key("GET", BASE + "/x?q=1", None) == f"GET {BASE}/x?q=1"


def test_replay_rejects_index_escape(tmp_path):
    sub = tmp_path / "r"
    sub.mkdir()
    (tmp_path / "secret.txt").write_bytes(b"top")
    (sub / "index.json").write_text(json.dumps(
        {f"GET {BASE}/x": {"file": "../secret.txt", "status": 200, "headers": {}}}), encoding="utf-8")
    op = replay.ReplayOpener(sub)
    with pytest.raises(urllib.error.URLError):
        op.open(urllib.request.Request(BASE + "/x"))


# ---- 9. proxy disclosure ---------------------------------------------------------------------------

def test_preview_states_proxy_use():
    text = approval.render_preview(make())
    assert "configured proxy" in text and "TLS stays end-to-end" in text


# ---- 11/12/13. CLI and strike ------------------------------------------------------------------------

def _cli(*args):
    return subprocess.run([sys.executable, "-I", str(SCRIPTS / "approval.py"), *args],
                          capture_output=True, text=True, encoding="utf-8")


def test_cli_strike_refuses_overwrite_and_bad_dirs(tmp_path):
    p, plan = written(tmp_path)
    existing = tmp_path / "exists.json"
    existing.write_text("keep", encoding="utf-8")
    out = _cli("strike", str(p), "--ids", "r1", "--out", str(existing))
    assert out.returncode == 2 and json.loads(out.stdout)["error"] == "output_exists"
    assert existing.read_text(encoding="utf-8") == "keep"
    out = _cli("strike", str(p), "--ids", "r1", "--out", str(p))
    assert out.returncode == 2 and json.loads(out.stdout)["error"] == "output_exists"
    out = _cli("strike", str(p), "--ids", "r1", "--out", str(tmp_path / "nodir" / "x.json"))
    assert out.returncode == 2 and "error" in json.loads(out.stdout) and "Traceback" not in out.stderr
    assert not (tmp_path / "nodir").exists()
    out = _cli("strike", str(p), "--ids", "r1", "--out", str(tmp_path))
    assert out.returncode == 2 and "Traceback" not in out.stderr
    out = _cli("preview", str(tmp_path))
    assert out.returncode == 2 and "Traceback" not in out.stderr


def test_strike_recomputes_cost_or_marks_stale():
    plan = make(cost={"requests": 3, "quota": "3 of 300/day", "other": "x"})
    new = approval.strike(plan, {"r3"})
    assert new["cost"]["requests"] == 2 and new["cost"]["quota"] == "2 of 300/day"
    assert new["cost"].get("stale") is True
    plan = make(cost={"requests": 3, "quota": "3 of 300/day"})
    new = approval.strike(plan, {"r3"})
    assert "stale" not in new["cost"]


def test_request_id_with_comma_rejected():
    with pytest.raises(ApprovalError) as e:
        make(requests=[dict(R1, id="a,b")], followups=[])
    assert code(e) == "plan_invalid"


def test_ambient_env_cannot_bypass(tmp_path, monkeypatch):
    for k in ("APPROVE", "SKIP_APPROVAL", "NO_PROXY", "FORCE"):
        monkeypatch.setenv(k, "1")
    g = guard_for([R1])
    with pytest.raises(ApprovalError):
        g.check("GET", BASE + "/other", None)
    assert os.environ["APPROVE"] == "1"


def test_reads_and_inflates_are_bounded_requests(monkeypatch):
    """The caps must bound memory, not just be checked afterwards."""
    seen = {"read": [], "inflate": []}

    class Spy(Resp):
        def read(self, n=-1):
            seen["read"].append(n)
            return super().read(n)

    real = net.zlib.decompressobj

    class DSpy:
        def __init__(self, *a):
            self._d = real(*a)

        def decompress(self, data, max_length=0):
            seen["inflate"].append(max_length)
            return self._d.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self._d, name)

    monkeypatch.setattr(net.zlib, "decompressobj", lambda *a: DSpy(*a))
    g = guard_for([R1])
    net.send("GET", R1["url"], g, opener=Opener(Spy(200, {"Content-Encoding": "gzip"}, gzip.compress(b"hi"))))
    assert seen["read"] == [net.MAX_RESPONSE_BYTES + 1]
    assert seen["inflate"] and all(0 < m <= net.MAX_RESPONSE_BYTES + 1 for m in seen["inflate"])
