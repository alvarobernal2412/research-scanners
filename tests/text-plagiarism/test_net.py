import io
import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import net
import pytest
from approval import Gate, PlannedRequest, UnapprovedRequest, build_plan
from conftest import FIXTURES
from net import Cache, RequestLog, Response, canonical_url, replay_opener, send

DOC = "d" * 64


class FakeResp(io.BytesIO):
    def __init__(self, status=200, body=b"{}", headers=None, url=None):
        super().__init__(body)
        self.status, self.headers, self.url = status, headers or {}, url

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return FakeResp(item.status, item.getvalue(), item.headers, item.url or request.full_url)


@pytest.fixture(autouse=True)
def _reset():
    net.reset_rate_limits()
    yield
    net.reset_rate_limits()


def req(rid="r001", service="openalex", url="https://api.openalex.org/works/doi:10.1/x", carries="public_id", **kw):
    return PlannedRequest(id=rid, service=service, method="GET", url=url, carries=carries, purpose="t", **kw)


def gate_for(*reqs, tier="strict"):
    return Gate(build_plan(tier, "r", DOC, list(reqs)))


def do(r, opener, tier="strict", **kw):
    kw.setdefault("sleep", lambda s: None)
    return send(r, gate_for(r, tier=tier), tier=tier, round_name="r", opener=opener, **kw)


def test_send_checks_gate_before_opening():
    r = req()
    opener = FakeOpener(FakeResp())
    with pytest.raises(UnapprovedRequest):
        send(req("r999"), gate_for(r), tier="strict", round_name="r", opener=opener, sleep=lambda s: None)
    changed = req(url="https://api.openalex.org/works/doi:10.1/other")
    with pytest.raises(UnapprovedRequest):
        send(changed, gate_for(r), tier="strict", round_name="r", opener=opener, sleep=lambda s: None)
    assert opener.calls == []


def test_secret_rendered_at_send_only(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENALEX_API_KEY", "k3y/value")
    r = req(url="https://api.openalex.org/works?api_key={OPENALEX_API_KEY}", identity="OPENALEX_API_KEY",
            headers={"X-Key": "{OPENALEX_API_KEY}"})
    opener = FakeOpener(FakeResp())
    log = RequestLog(tmp_path / "log.jsonl")
    resp = do(r, opener, log=log)
    assert resp.status == 200
    assert "api_key=k3y%2Fvalue" in opener.calls[0].full_url
    assert opener.calls[0].get_header("X-key") == "k3y/value"
    logged = (tmp_path / "log.jsonl").read_text(encoding="utf-8")
    assert "k3y" not in logged and "{OPENALEX_API_KEY}" in logged


def test_missing_secret_is_error_not_send(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    r = req(service="brave", url="https://api.search.brave.com/x", headers={"X-Subscription-Token": "{BRAVE_API_KEY}"},
            identity="BRAVE_API_KEY")
    opener = FakeOpener(FakeResp())
    resp = do(r, opener)
    assert resp.error == "missing_key" and resp.status is None and opener.calls == []


def test_429_honours_retry_after_then_succeeds():
    sleeps = []
    opener = FakeOpener(FakeResp(429, headers={"Retry-After": "3"}), FakeResp(200, b"ok"))
    resp = do(req(), opener, sleep=sleeps.append)
    assert resp.status == 200 and resp.body == b"ok" and resp.error is None
    assert 3 in sleeps and len(opener.calls) == 2


def test_core_iso_retry_after_parsed():
    when = (datetime.now(timezone.utc) + timedelta(seconds=7)).isoformat()
    sleeps = []
    opener = FakeOpener(FakeResp(429, headers={"x-ratelimit-retry-after": when}), FakeResp(200, b"{}"))
    resp = do(req(service="core", url="https://api.core.ac.uk/v3/search/works/?q=x"), opener, sleep=sleeps.append)
    assert resp.status == 200
    assert any(5 <= s <= 7.5 for s in sleeps)


def test_retry_after_over_cap_gives_http_429():
    sleeps = []
    opener = FakeOpener(FakeResp(429, headers={"Retry-After": "600"}))
    resp = do(req(), opener, sleep=sleeps.append)
    assert resp.error == "http_429" and resp.status == 429 and 600 not in sleeps and len(opener.calls) == 1


def test_429_retries_are_bounded():
    opener = FakeOpener(FakeResp(429, headers={"Retry-After": "1"}))
    resp = do(req(), opener)
    assert resp.error == "http_429" and len(opener.calls) == net.MAX_RETRIES + 1


def test_500_and_timeout_map_to_errors_without_raising():
    assert do(req(), FakeOpener(FakeResp(500))).error == "http_5xx"
    assert do(req(), FakeOpener(FakeResp(404))).error == "http_4xx"
    assert do(req(), FakeOpener(TimeoutError("slow"))).error == "timeout"
    assert do(req(), FakeOpener(urllib.error.URLError(TimeoutError("timed out")))).error == "timeout"
    assert do(req(), FakeOpener(urllib.error.URLError("dns"))).error == "network_error"
    http_err = urllib.error.HTTPError("https://x", 503, "unavailable", {}, io.BytesIO(b"x"))
    assert do(req(), FakeOpener(http_err)).error == "http_5xx"


def test_size_cap_too_large(monkeypatch):
    monkeypatch.setattr(net, "MAX_DOWNLOAD_BYTES", 10)
    resp = do(req(), FakeOpener(FakeResp(200, b"x" * 11)))
    assert resp.error == "too_large" and resp.body == b""


def test_min_interval_per_service():
    sleeps = []
    r1, r2 = req("r001", "crossref", "https://api.crossref.org/works?a=1"), req("r002", "crossref", "https://api.crossref.org/works?a=2")
    gate = gate_for(r1, r2)
    opener = FakeOpener(FakeResp())
    send(r1, gate, tier="strict", round_name="r", opener=opener, sleep=sleeps.append)
    assert sleeps == []
    send(r2, gate, tier="strict", round_name="r", opener=opener, sleep=sleeps.append)
    assert len(sleeps) == 1 and 0.2 < sleeps[0] <= 0.34
    assert net.MIN_INTERVAL == {"crossref": 0.34, "openalex": 0.2, "zenodo": 0.5, "arxiv": 3.0, "europepmc": 0.2,
                                "core": 6.0, "openlibrary": 1.0, "brave": 1.0, "web": 1.0}


def test_cache_hit_skips_opener_and_logs_from_cache(tmp_path):
    cache, log = Cache(tmp_path / "cache"), RequestLog(tmp_path / "log.jsonl")
    r = req()
    first = do(r, FakeOpener(FakeResp(200, b"payload")), cache=cache, log=log)
    assert not first.from_cache
    boom = FakeOpener(AssertionError("must not be called"))
    second = do(r, boom, cache=cache, log=log)
    assert second.from_cache and second.body == b"payload" and boom.calls == []
    entries = [json.loads(l) for l in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["from_cache"] for e in entries] == [False, True]
    assert set(entries[0]) >= {"time", "request_id", "service", "tier", "round", "url", "text_sent", "identity",
                               "status", "error", "final_url", "from_cache"}


def test_cache_stores_only_200(tmp_path):
    cache = Cache(tmp_path)
    r = req()
    do(r, FakeOpener(FakeResp(500)), cache=cache)
    assert cache.get(r) is None


def test_cache_root_env_override_and_not_in_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("TEXT_PLAGIARISM_CACHE", str(tmp_path / "c"))
    assert net.cache_root() == tmp_path / "c"
    monkeypatch.delenv("TEXT_PLAGIARISM_CACHE")
    root = net.cache_root()
    repo = FIXTURES.parents[2]
    assert repo not in root.parents and root.name == "text-plagiarism"


def test_redirect_final_url_logged(tmp_path):
    log = RequestLog(tmp_path / "log.jsonl")
    opener = FakeOpener(FakeResp(302, headers={"Location": "https://zenodo.org/api/records/2"}),
                        FakeResp(200, b"{}"))
    resp = do(req(service="zenodo", url="https://zenodo.org/api/records/1"), opener, log=log)
    assert resp.status == 200 and resp.final_url == "https://zenodo.org/api/records/2"
    assert json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8"))["final_url"] == resp.final_url


def test_redirect_not_followed_for_user_text():
    r = PlannedRequest("r001", "europepmc", "GET", "https://www.ebi.ac.uk/x?query=%22a%20b%22", "user_text", "t",
                       text_sent="a b")
    opener = FakeOpener(FakeResp(302, headers={"Location": "https://evil.example/leak"}))
    resp = do(r, opener, tier="open")
    assert resp.error == "redirect_blocked" and len(opener.calls) == 1


def test_replay_opener_serves_recorded_and_rejects_unknown():
    opener = replay_opener(FIXTURES / "responses")
    url = "https://api.openalex.org/works/doi:10.2196/27499"
    r = req(url=url)
    resp = do(r, opener)
    assert resp.status == 200 and b"openalex.org/W3203031374" in resp.body
    bad = req(url="https://api.openalex.org/works/doi:10.0000/none")
    assert do(bad, opener).error == "network_error"
    z = do(req(service="zenodo", url="https://zenodo.org/api/records/21373046"), opener)
    assert z.final_url == "https://zenodo.org/api/records/21373047"


def test_canonical_url_ignores_param_order():
    a = canonical_url("https://API.example.org/p?b=2&a=1%20x#frag")
    b = canonical_url("https://api.example.org/p?a=1+x&b=2")
    assert a == b == "https://api.example.org/p?a=1%20x&b=2"


def test_unsupported_scheme_not_sent():
    opener = FakeOpener(FakeResp())
    resp = do(req(url="file:///etc/passwd"), opener)
    assert resp.error == "network_error" and opener.calls == []
