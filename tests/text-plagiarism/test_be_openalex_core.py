import base64
import io
import json
import urllib.error

import be_core
import be_openalex
import open_tier
import prepare
from conftest import FIXTURES
from helpers import call_main, write_md
from net import canonical_url, replay_opener

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))
A = P["workA"]
D1 = P["pmc"]["sentences"][0]
INDEX = json.loads((FIXTURES / "responses" / "index.json").read_text(encoding="utf-8"))
REPLAY = replay_opener(FIXTURES / "responses")


def recorded(url):
    name = INDEX[canonical_url(url)]
    return base64.b64decode(json.loads((FIXTURES / "responses" / name).read_text(encoding="utf-8"))["body_b64"])


def test_openalex_url_quotes_phrase_and_drops_commas():
    phrase = open_tier.phrase_for_query(A["control_sentence"], "openalex")
    assert phrase == "The events that do not share any data attributes and/or values or"
    req = be_openalex.build_search(phrase, "r001")
    assert canonical_url(req.url) in INDEX                              # exactly the recorded request
    assert "%22" in req.url and "%2C" not in req.url and req.carries == "user_text" and req.identity is None
    assert "," not in be_openalex.build_search("a, b | c.", "r").text_sent
    assert be_openalex.window_phrase("one, two three.") == "one two three"


def test_openalex_second_window():
    words = [f"w{i}" for i in range(30)]
    s = " ".join(words)
    assert be_openalex.window_phrase(s).split() == words[:12]
    assert be_openalex.second_window(s).split() == words[-12:]
    assert be_openalex.second_window(" ".join(words[:20])).split() == words[4:16]
    assert be_openalex.second_window(" ".join(words[:13])) is None


def test_openalex_parse_recorded_control_single_hit():
    req = be_openalex.build_search(open_tier.phrase_for_query(A["control_sentence"], "openalex"), "r1")
    count, hits = be_openalex.parse_search(recorded(req.url), "c1")
    assert count == 1 and len(hits) == 1
    h = hits[0]
    assert h.doi == A["doi"] and h.license == "CC BY" and h.fulltext_url.endswith("s12599-020-00641-4.pdf")
    assert h.backend == "openalex"


def test_openalex_hits_noted_stemmed():
    req = be_openalex.build_search(open_tier.phrase_for_query(A["control_sentence"], "openalex"), "r1")
    _, hits = be_openalex.parse_search(recorded(req.url), "c1")
    assert all("stemmed_search" in h.notes for h in hits)


def test_openalex_parse_t1_zero():
    req = be_openalex.build_search(open_tier.phrase_for_query(A["miss_sentence"], "openalex"), "r1")
    assert be_openalex.parse_search(recorded(req.url), "c1") == (0, [])


def test_openalex_key_param_only_with_use_keys(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k-123456")
    plain = be_openalex.build_search("some words here", "r1", use_key=False)
    keyed = be_openalex.build_search("some words here", "r1", use_key=True)
    assert "api_key" not in plain.url and plain.identity is None
    assert keyed.url.endswith("&api_key={OPENALEX_API_KEY}") and keyed.identity == "OPENALEX_API_KEY"
    assert "k-123456" not in keyed.url
    monkeypatch.delenv("OPENALEX_API_KEY")
    assert "api_key" not in be_openalex.build_search("some words here", "r1", use_key=True).url


class FakeResp(io.BytesIO):
    def __init__(self, status, body, headers):
        super().__init__(body)
        self.status, self.headers = status, headers

    def geturl(self):
        return None


def test_openalex_budget_stop_marks_rest_budget_exhausted(tmp_path):
    sentences = [f"Qwertyuiop{c} zxcvbnmasd lkjhgfdsa pqowieurty mnbvcxzlkj hgfdsapoiu ytrewqlkjh gfdsamnbvc xzlkjhpoiu "
                 f"ytrewqazxs wedcrfvtgb yhnujmikol." for c in "abc"]
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", "# Introduction\n\n" + " ".join(sentences) + "\n")),
                             "--workdir", str(wd)])
    code, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1", "--backends", "openalex"])
    assert code == 0 and plan["preview"]["services"]["openalex"]["count"] == 3
    calls = []

    def opener(request, timeout=None):
        calls.append(request.full_url)
        return FakeResp(200, b'{"meta": {"count": 0}, "results": []}', {"x-ratelimit-remaining": "5"})

    code, res = call_main(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"],
                                           "--sha256", plan["sha256"]], opener=opener)
    assert code == 0 and len(calls) == 1 and res["errors"] == {"budget_exhausted": 2}
    state = json.loads((wd / "open_state.json").read_text(encoding="utf-8"))
    errs = sorted(str(r["error"]) for r in state["results"].values())
    assert errs == ["None", "budget_exhausted", "budget_exhausted"]
    assert be_openalex.budget_remaining({"x-ratelimit-remaining": "9"}) == 9
    assert be_openalex.budget_remaining({}) is None


def test_second_window_round_for_zero_hit_openalex_queries(tmp_path):
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", f"# Introduction\n\n{D1}\n")), "--workdir", str(wd)])
    code, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1", "--backends", "openalex"])
    code, res = call_main(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"],
                                           "--sha256", plan["sha256"]], opener=REPLAY)
    assert code == 0 and res["errors"] == {}
    code, nxt = call_main(open_tier.main, ["plan", "--workdir", str(wd)])
    assert nxt["round"] == "search2"
    req = json.loads(open(nxt["plan"], encoding="utf-8").read())["requests"][0]
    words = D1.rstrip(".").replace(",", " ").split()
    assert req["text_sent"] == " ".join(words[4:16]) and req["text_sent"] != " ".join(words[:12])
    assert req["service"] == "openalex" and req["carries"] == "user_text"


def test_core_url_parenthesised_phrase_trailing_slash():
    req = be_core.build_search(A["control_sentence"], "r1")
    assert canonical_url(req.url) in INDEX
    assert req.url.startswith("https://api.core.ac.uk/v3/search/works/?q=%28%22") and "%22%29&limit=5" in req.url
    assert req.headers == {} and req.identity is None and be_core.KEYED_ONLY is False


def test_core_parse_recorded_responses():
    count, hits = be_core.parse_search(recorded(be_core.build_search(A["control_sentence"], "r1").url), "c1")
    assert (count, hits) == (0, [])
    body = json.dumps({"totalHits": 1, "results": [{"id": 77, "doi": "https://doi.org/10.1/X", "title": "T",
                                                      "downloadUrl": "https://core.ac.uk/download/77.pdf",
                                                      "fullText": "inline text"}]}).encode()
    count, hits = be_core.parse_search(body, "c1")
    assert count == 1 and hits[0].doi == "10.1/x" and hits[0].full_text == "inline text"
    assert hits[0].fulltext_url.endswith("77.pdf") and "core_body_text_unverified" in hits[0].notes


def test_core_429_is_an_error_not_a_crash(tmp_path):
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", f"# Introduction\n\n{D1}\n")), "--workdir", str(wd)])
    code, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1", "--backends", "core"])
    assert code == 0

    def limited(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "slow down", {"Retry-After": "900"}, io.BytesIO(b""))

    code, res = call_main(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"],
                                           "--sha256", plan["sha256"]], opener=limited)
    assert code == 0 and res["errors"] == {"http_429": 1}
    state = json.loads((wd / "open_state.json").read_text(encoding="utf-8"))
    assert [r["error"] for r in state["results"].values()] == ["http_429"]


def test_core_bearer_header_only_with_use_keys(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "core-key-123")
    assert be_core.build_search("some words", "r1", use_key=False).headers == {}
    keyed = be_core.build_search("some words", "r1", use_key=True)
    assert keyed.headers == {"Authorization": "Bearer {CORE_API_KEY}"} and keyed.identity == "CORE_API_KEY"
    assert "core-key-123" not in json.dumps(keyed.headers)


def test_core_is_opt_in_not_default():
    assert "core" in open_tier.BACKENDS and "core" not in open_tier.DEFAULT_BACKENDS
    assert open_tier.DEFAULT_BACKENDS == ("openalex", "europepmc")
