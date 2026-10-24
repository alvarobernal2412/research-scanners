import base64
import io
import json
import urllib.error

import be_europepmc as be
import net
import open_tier
import prepare
from conftest import FIXTURES
from helpers import call_main, write_md
from net import canonical_url, replay_opener

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))
INDEX = json.loads((FIXTURES / "responses" / "index.json").read_text(encoding="utf-8"))
D1, D3 = P["pmc"]["sentences"]


def recorded(url):
    name = INDEX[canonical_url(url)]
    return base64.b64decode(json.loads((FIXTURES / "responses" / name).read_text(encoding="utf-8"))["body_b64"])


def test_be_europepmc_build_url_matches_endpoint():
    req = be.build_search(D1, "r001")
    assert req.service == "europepmc" and req.carries == "user_text" and req.text_sent == D1 and req.identity is None
    assert canonical_url(req.url) in INDEX                      # exactly the recorded request
    assert req.url.startswith("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=%22")
    assert be.KEYED_ONLY is False and be.SERVICE == "europepmc"
    quoted = be.build_search('say "hi" now', "r002")
    assert "%5C%22hi%5C%22" in quoted.url                       # inner double quotes are escaped


def test_be_europepmc_parse_recorded_control_hit():
    count, hits = be.parse_search(recorded(be.build_search(D1, "r1").url), "c1")
    assert count == 1 and len(hits) == 1
    h = hits[0]
    assert h.pmcid == P["pmc"]["pmcid"] and h.doi == P["pmc"]["doi"] and h.license == "CC BY"
    assert h.fulltext_url == "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC8529480/fullTextXML"
    assert h.backend == "europepmc" and h.query_id == "c1" and h.snippet is None


def test_be_europepmc_parse_zero_hits():
    miss = P["workA"]["miss_sentence"]
    count, hits = be.parse_search(recorded(be.build_search(miss, "r1").url), "c1")
    assert count == 0 and hits == []


def test_apostrophe_variants_both_queried():
    sentence = P["apostrophe"]["sentence"]
    straight, curly = be.variants(sentence)
    assert "'" in straight and "’" in curly and be.variants("no apostrophe here") == ["no apostrophe here"]
    assert be.parse_search(recorded(be.build_search(straight, "r1").url), "c")[0] == 0   # recorded: straight misses
    count, hits = be.parse_search(recorded(be.build_search(curly, "r2").url), "c")
    assert count == 1 and hits[0].pmcid == P["apostrophe"]["pmcid"]


def _plan_and_run(tmp_path, opener):
    md = f"# Introduction\n\n{D1} {D3}\n"
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", md)), "--workdir", str(wd)])
    code, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1", "--backends", "europepmc"])
    assert code == 0, plan
    code, res = call_main(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"],
                                           "--sha256", plan["sha256"]], opener=opener)
    assert code == 0, res
    return wd, plan, res


def test_run_records_errors_per_query(tmp_path):
    replay = replay_opener(FIXTURES / "responses")

    def flaky(request, timeout=None):
        if "advantage" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b""))
        return replay(request, timeout)

    wd, plan, res = _plan_and_run(tmp_path, flaky)
    assert res["round"] == "search" and res["errors"] == {"http_5xx": 1}
    state = json.loads((wd / "open_state.json").read_text(encoding="utf-8"))
    results = list(state["results"].values())
    by_err = {r["error"]: r for r in results}
    assert set(by_err) == {"http_5xx", None}
    assert by_err[None]["hit_count"] == 1 and by_err[None]["hits"][0]["pmcid"] == "PMC8529480"
    assert by_err["http_5xx"]["hit_count"] is None
    log = [json.loads(l) for l in (wd / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {e["tier"] for e in log} == {"open"} and {e["carries"] for e in log} == {"user_text"}
    assert sorted(e["text_sent"] for e in log) == sorted([D1, D3])


def test_run_with_wrong_hash_sends_nothing(tmp_path):
    md = f"# Introduction\n\n{D1}\n"
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", md)), "--workdir", str(wd)])
    _, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1"])

    def boom(*a, **k):
        raise AssertionError("network must not be touched")

    code, out = call_main(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"], "--sha256", "1" * 64],
                          opener=boom)
    assert code == 2 and out["error"] == "plan_hash_mismatch" and not (wd / "requests.jsonl").exists()
