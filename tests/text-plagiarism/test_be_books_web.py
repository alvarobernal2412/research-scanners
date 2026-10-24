import base64
import io
import json

import be_books_web as bw
import open_tier
import prepare
from conftest import FIXTURES
from helpers import call_main, write_md
from net import canonical_url
from verify import open_findings

from test_verify import fake_hit, synthetic_state, verify_round

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))
INDEX = json.loads((FIXTURES / "responses" / "index.json").read_text(encoding="utf-8"))
D1 = P["pmc"]["sentences"][0]
FAMOUS = "it was the best of times, it was the worst of times"


def test_openlibrary_parse_recorded_famous_sentence_hits():
    req = bw.openlibrary.build_search(FAMOUS, "r1")
    assert canonical_url(req.url) in INDEX and req.carries == "user_text" and req.identity is None
    name = INDEX[canonical_url(req.url)]
    body = base64.b64decode(json.loads((FIXTURES / "responses" / name).read_text(encoding="utf-8"))["body_b64"])
    count, hits = bw.openlibrary.parse_search(body, "c1")
    assert count > 1000 and hits
    h = hits[0]
    assert "{{{" in h.snippet and h.title and h.url.startswith("https://archive.org/details/")
    assert h.fulltext_url is None and h.full_text is None and "snippet_only" in h.notes
    assert bw.openlibrary.KEYED_ONLY is False


def test_openlibrary_hits_never_findings(tmp_path):
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", f"# Introduction\n\n{D1}\n")), "--workdir", str(wd)])
    synthetic_state(wd, D1, fake_hit(backend="openlibrary", snippet="... {{{process mining has an advantage}}} ...",
                                     url="https://archive.org/details/x", title="A Book"))
    res = open_findings(wd)
    assert res["findings"] == [] and res["informational"] == []
    assert res["unverified_candidates"][0]["backend"] == "openlibrary"


def test_brave_requires_key_and_preview_shows_identity(tmp_path, monkeypatch):
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", f"# Introduction\n\n{D1}\n")), "--workdir", str(wd)])
    code, out = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1", "--backends", "brave"])
    assert code == 2 and out == {"error": "missing_key", "detail": "BRAVE_API_KEY"}
    monkeypatch.setenv("BRAVE_API_KEY", "brave-secret-xyz")
    code, out = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1", "--backends", "brave"])
    assert code == 0
    svc = out["preview"]["services"]["brave"]
    assert svc["identity"] == ["BRAVE_API_KEY"] and svc["carries"] == ["user_text"]
    text = open(out["plan"], encoding="utf-8").read()
    assert "brave-secret-xyz" not in text and "{BRAVE_API_KEY}" in text and "X-Subscription-Token" in text


def test_brave_parse_documented_shape():
    # UNVERIFIED shape: synthetic, built from Brave's documented response; no recording exists without a key.
    body = json.dumps({"web": {"results": [{"url": "https://example.org/a", "title": "A page", "description": "snip"},
                                           {"title": "no url"}]}}).encode()
    count, hits = bw.brave.parse_search(body, "c1")
    assert count == 1 and hits[0].fulltext_url == "https://example.org/a" and hits[0].snippet == "snip"
    req = bw.brave.build_search("some words here", "r1")
    assert req.headers == {"X-Subscription-Token": "{BRAVE_API_KEY}"} and req.identity == "BRAVE_API_KEY"
    assert bw.brave.KEYED_ONLY is True and bw.brave.KEY_ENV == "BRAVE_API_KEY"
    assert req.url.startswith("https://api.search.brave.com/res/v1/web/search?q=%22") and req.url.endswith("&count=5")


class Resp(io.BytesIO):
    def __init__(self, body, ctype):
        super().__init__(body)
        self.status, self.headers = 200, {"Content-Type": ctype}

    def geturl(self):
        return None


def test_brave_hit_page_verified_by_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "brave-secret-xyz")
    page = ("<html><head><title>Blog</title></head><body><h1>Notes on process mining</h1><p>Intro words here. "
            f"{D1} Closing words of the blog post.</p><script>var x=1;</script></body></html>")
    api = json.dumps({"web": {"results": [{"url": "https://blog.example.org/post", "title": "Blog post",
                                           "description": "..."}]}}).encode()

    def opener(request, timeout=None):
        if "api.search.brave.com" in request.full_url:
            assert request.get_header("X-subscription-token") == "brave-secret-xyz"
            return Resp(api, "application/json")
        assert request.full_url == "https://blog.example.org/post"
        return Resp(page.encode("utf-8"), "text/html")

    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", f"# Introduction\n\n{D1}\n")), "--workdir", str(wd)])
    code, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1", "--backends", "brave"])
    code, res = call_main(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"],
                                           "--sha256", plan["sha256"]], opener=opener)
    assert code == 0
    vplan = verify_round(wd, opener)
    assert vplan["preview"]["services"]["web"]["carries"] == ["public_url"]
    [f] = open_findings(wd)["findings"]
    assert f["source"]["found_via"] == "open:brave" and f["source"]["license"] is None
    assert f["source"]["url"] == "https://blog.example.org/post" and f["attribution"] == "unattributed"
    log = (wd / "requests.jsonl").read_text(encoding="utf-8")
    assert "brave-secret-xyz" not in log
