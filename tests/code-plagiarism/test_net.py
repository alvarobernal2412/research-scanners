"""Guarded transport and replay tooling. No network."""
import gzip
import hashlib
import io
import json
import sys
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

BASE = "https://api.stackexchange.com/2.3"
URL = BASE + "/search/excerpts?q=alpha&site=stackoverflow"
REQ = {"id": "r1", "method": "GET", "url": URL, "body": None, "purpose": "p", "source": None}
POST = {"id": "r2", "method": "POST", "url": "https://api.osskb.org/scan/direct",
        "body": "wfp-data", "purpose": "p", "source": None}


def guard(requests=None, followups=None):
    plan = approval.build_plan("stackexchange", "open", "root", requests or [REQ, POST], followups or [],
                               [], {}, [], {})
    return Guard(plan)


class Resp:
    def __init__(self, status=200, headers=None, body=b"{}"):
        self.status, self.headers, self._b = status, headers or {}, body

    def read(self, n=-1):
        return self._b if n is None or n < 0 else self._b[:n]

    def close(self):
        pass

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def open(self, req, timeout=None):
        self.calls.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class BoomOpener:
    def open(self, *a, **k):
        raise AssertionError("opener must not be called")


def test_send_checks_guard_before_opener():
    g = guard()
    with pytest.raises(ApprovalError) as e:
        net.send("GET", URL + "&x=1", g, opener=BoomOpener())
    assert e.value.code == "request_not_in_plan"
    with pytest.raises(ApprovalError):
        net.send("POST", POST["url"], g, body=b"other", opener=BoomOpener())
    with pytest.raises(ApprovalError):
        net.send("GET", URL, g, headers={"Authorization": "Bearer x"}, opener=BoomOpener())
    with pytest.raises(ApprovalError):
        net.send("GET", URL, g, headers={"Cookie": "a=b"}, opener=BoomOpener())
    assert g.sent == []


def test_send_plain_and_headers_and_user_agent():
    op = Opener(Resp(200, {"Content-Type": "application/json"}, b'{"a":1}'))
    r = net.send("GET", URL, guard(), opener=op)
    assert (r.status, r.body) == (200, b'{"a":1}') and r.headers["content-type"] == "application/json"
    req = op.calls[0]
    assert req.full_url == URL and req.get_method() == "GET" and req.data is None
    names = {k.lower() for k, _ in req.header_items()}
    assert names == {"user-agent"}
    assert req.get_header("User-agent") == net.USER_AGENT


def test_send_post_body_exact():
    op = Opener(Resp())
    net.send("POST", POST["url"], guard(), body=b"wfp-data", opener=op)
    assert op.calls[0].data == b"wfp-data" and op.calls[0].get_method() == "POST"


def test_send_planned_headers_and_secret_from_env(monkeypatch):
    monkeypatch.setenv("STACKEXCHANGE_KEY", "topsecret")
    req = dict(REQ, headers={"Accept": "application/json", "X-Key": {"secret_from_env": "STACKEXCHANGE_KEY"}})
    op = Opener(Resp())
    net.send("GET", URL, guard([req]), opener=op)
    sent = {k.lower(): v for k, v in op.calls[0].header_items()}
    assert sent == {"user-agent": net.USER_AGENT, "accept": "application/json", "x-key": "topsecret"}
    monkeypatch.delenv("STACKEXCHANGE_KEY")
    with pytest.raises(net.NetError) as e:
        net.send("GET", URL, guard([req]), opener=BoomOpener())
    assert e.value.code == "secret_missing"
    monkeypatch.setenv("STACKEXCHANGE_KEY", "bad\r\nvalue")
    with pytest.raises(net.NetError) as e:
        net.send("GET", URL, guard([req]), opener=BoomOpener())
    assert "bad" not in str(e.value)


def test_send_gunzips():
    body = gzip.compress(b'{"items": []}')
    r = net.send("GET", URL, guard(), opener=Opener(Resp(200, {"Content-Encoding": "gzip"}, body)))
    assert r.body == b'{"items": []}'


def test_send_retries_429_bounded():
    sleeps = []
    op = Opener(Resp(429, {"Retry-After": "999"}), Resp(503, {"Retry-After": "2"}), Resp(200, {}, b"ok"))
    r = net.send("GET", URL, guard(), opener=op, sleep=sleeps.append)
    assert r.status == 200 and sleeps == [30, 2] and len(op.calls) == 3
    op = Opener(Resp(429), Resp(429), Resp(429), Resp(200))
    sleeps.clear()
    r = net.send("GET", URL, guard(), opener=op, sleep=sleeps.append)
    assert r.status == 429 and len(op.calls) == 1 + net.MAX_RETRIES and all(0 <= s <= 30 for s in sleeps)
    sleeps.clear()
    op = Opener(Resp(429, {"Retry-After": "junk"}), Resp(200))
    net.send("GET", URL, guard(), opener=op, sleep=sleeps.append)
    assert 0 <= sleeps[0] <= 30


def test_send_http_error_returns_response():
    err = urllib.error.HTTPError(URL, 404, "nf", {"X": "y"}, io.BytesIO(b"nope"))
    r = net.send("GET", URL, guard(), opener=Opener(err))
    assert r.status == 404 and r.body == b"nope"


def test_send_url_error_raises_neterror():
    with pytest.raises(net.NetError) as e:
        net.send("GET", URL, guard(), opener=Opener(urllib.error.URLError("dns")))
    assert e.value.code == "network_error"
    with pytest.raises(net.NetError) as e:
        net.send("GET", URL, guard(), opener=Opener(TimeoutError("t")))
    assert e.value.code == "network_error"


def test_redirect_is_blocked():
    with pytest.raises(net.NetError) as e:
        net.send("GET", URL, guard(), opener=Opener(Resp(302, {"Location": "https://evil.example/x"})))
    assert e.value.code == "redirect_blocked"
    err = urllib.error.HTTPError(URL, 301, "moved", {"Location": "https://evil.example/"}, io.BytesIO(b""))
    with pytest.raises(net.NetError) as e:
        net.send("GET", URL, guard(), opener=Opener(err))
    assert e.value.code == "redirect_blocked"


def test_default_opener_does_not_follow_redirects():
    op = net.build_default_opener()
    handlers = [h for h in op.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert handlers and handlers[0].redirect_request(
        urllib.request.Request(URL), None, 302, "x", {}, "https://evil.example/") is None
    assert not [h for h in op.handlers if type(h).__name__ in ("FileHandler", "FTPHandler", "DataHandler")]


def test_guard_sent_log():
    g = guard()
    net.send("GET", URL, g, opener=Opener(Resp()))
    net.send("POST", POST["url"], g, body=b"wfp-data", opener=Opener(Resp()))
    assert [(s["method"], s["url"]) for s in g.sent] == [("GET", URL), ("POST", POST["url"])]
    assert g.sent[1]["body_sha256"] == hashlib.sha256(b"wfp-data").hexdigest()
    assert "wfp-data" not in json.dumps(g.sent)


def test_followup_through_send():
    fu = {"method": "GET", "url_template": BASE + "/answers/{ids}?site=stackoverflow", "id_param": "ids",
          "max_ids": 5, "max_requests": 5}
    g = guard(followups=[fu])
    with pytest.raises(ApprovalError):
        net.send("GET", BASE + "/answers/9?site=stackoverflow", g, opener=BoomOpener())
    g.allow_ids([9])
    net.send("GET", BASE + "/answers/9?site=stackoverflow", g, opener=Opener(Resp()))


# ---- replay -----------------------------------------------------------------

def test_replay_unrecorded_raises(tmp_path):
    (tmp_path / "index.json").write_text("{}", encoding="utf-8")
    op = replay.ReplayOpener(tmp_path)
    with pytest.raises(net.NetError) as e:
        net.send("GET", URL, guard(), opener=op)
    assert e.value.code == "network_error"
    with pytest.raises(urllib.error.URLError):
        op.open(urllib.request.Request(URL))
    assert op.requests


def test_recording_then_replay_roundtrip(tmp_path):
    real = Opener(Resp(200, {"Content-Encoding": "gzip", "Content-Type": "application/json"},
                       gzip.compress(b'{"items":[1]}')),
                  Resp(200, {}, b"wfp-reply"))
    rec = replay.RecordingOpener(real, tmp_path)
    g = guard()
    r1 = net.send("GET", URL, g, opener=rec)
    assert r1.body == b'{"items":[1]}'
    net.send("POST", POST["url"], g, body=b"wfp-data", opener=rec)
    idx = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert f"GET {URL}" in idx and f"POST {POST['url']} {hashlib.sha256(b'wfp-data').hexdigest()}" in idx
    rep = replay.ReplayOpener(tmp_path)
    g2 = guard()
    assert net.send("GET", URL, g2, opener=rep).body == b'{"items":[1]}'
    r = net.send("POST", POST["url"], g2, body=b"wfp-data", opener=rep)
    assert r.body == b"wfp-reply" and r.status == 200
    assert len(rep.requests) == 2


def test_replay_strips_key_param_and_status(tmp_path):
    (tmp_path / "a.json").write_bytes(b"{}")
    idx = {f"GET {BASE}/x?q=1": {"file": "a.json", "status": 200, "headers": {"X-A": "b"}},
           f"GET {BASE}/y": {"file": "a.json", "status": 404, "headers": {}}}
    (tmp_path / "index.json").write_text(json.dumps(idx), encoding="utf-8")
    op = replay.ReplayOpener(tmp_path)
    r = op.open(urllib.request.Request(BASE + "/x?q=1&key=SECRET"))
    assert r.read() == b"{}" and r.status == 200
    r = op.open(urllib.request.Request(BASE + "/x?key=SECRET&q=1"))
    assert r.status == 200
    with pytest.raises(urllib.error.HTTPError) as e:
        op.open(urllib.request.Request(BASE + "/y"))
    assert e.value.code == 404
    assert not any("SECRET" in str(x) for x in op.requests)


def test_scanoss_replay_runner(tmp_path):
    by = tmp_path / "by.json"
    by.write_text(json.dumps({"aaa": [{"id": "file", "purl": ["pkg:x"]}]}), encoding="utf-8")
    wfp = tmp_path / "x.wfp"
    wfp.write_text("file=aaa,0,1.js\n10=abcd\nfile=bbb,0,2.py\n", encoding="utf-8")
    out = tmp_path / "out.json"
    runner = replay.ScanossReplayRunner(by)
    rc, so, se = runner(["scanoss-py", "scan", "--wfp", str(wfp), "-o", str(out)], {}, str(tmp_path))
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"1.js": [{"id": "file", "purl": ["pkg:x"]}], "2.py": [{"id": "none"}]}
