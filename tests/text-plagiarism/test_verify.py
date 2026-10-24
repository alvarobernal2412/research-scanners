import base64
import json
from dataclasses import asdict

import open_tier
import prepare
from conftest import FIXTURES
from extract import extract_bytes
from helpers import call_main, write_md
from net import replay_opener
from open_tier import Candidate
from verify import open_findings, verify_sentence

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))
A = P["workA"]
D1, D3 = P["pmc"]["sentences"]
REPLAY = replay_opener(FIXTURES / "responses")


def run_open(tmp_path, md, opener=REPLAY, backends="europepmc", include=""):
    wd = tmp_path / "wd"
    code, out = call_main(prepare.main, [str(write_md(tmp_path, "d.md", md)), "--workdir", str(wd)])
    assert code == 0, out
    code, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1", "--backends", backends,
                                           "--include", include])
    assert code == 0, plan
    code, res = call_main(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"],
                                           "--sha256", plan["sha256"]], opener=opener)
    assert code == 0, res
    return wd


def verify_round(wd, opener=REPLAY):
    code, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd)])
    assert code == 0, plan
    if plan.get("done"):
        return plan
    assert plan["round"] == "verify", plan
    code, res = call_main(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"],
                                           "--sha256", plan["sha256"]], opener=opener)
    assert code == 0, res
    return plan


def synthetic_state(wd, candidate_text, hit, hit_count=1):
    doc = json.loads((wd / "doc.json").read_text(encoding="utf-8"))
    lines = {l["no"]: l["text"] for l in doc["lines"]}
    line_no = next(n for n, t in lines.items() if candidate_text[:30] in t)
    cand = Candidate("c1", "s1", line_no, line_no, candidate_text, 1.0, None)
    state = {"version": 1, "use_keys": False,
             "selection": {"sections": ["s1"], "backends": ["europepmc"], "cap": 10, "candidates": [asdict(cand)],
                           "excluded": [], "over_cap": {}, "backend_over_cap": {}},
             "queries": {"1:r001": {"candidate": "c1", "backend": hit.get("backend", "europepmc"),
                                     "text": candidate_text, "req_id": "r001"}},
             "results": {"1:r001": {"hit_count": hit_count, "hits": [hit], "error": None}},
             "rounds": [{"round": "search"}]}
    (wd / "open_state.json").write_text(json.dumps(state), encoding="utf-8")
    return cand


def fake_hit(**kw):
    base = {"query_id": "c1", "backend": "europepmc", "doi": "10.1/fake", "title": "A fake paper", "url": "https://x.org/p",
            "license": "CC BY", "pmcid": None, "fulltext_url": None, "full_text": None, "snippet": None, "notes": []}
    base.update(kw)
    return base


def pdf_a_text():
    rec = json.loads((FIXTURES / "responses" / "pdf-A.json").read_text(encoding="utf-8"))
    doc = extract_bytes(base64.b64decode(rec["body_b64"]), "pdf")
    return "\n".join(l.text for l in doc.lines)


def test_europepmc_control_verified_with_doi_and_license(tmp_path):
    wd = run_open(tmp_path, f"# Introduction\n\n{D1}\n")
    plan = verify_round(wd)
    assert plan["preview"]["services"]["europepmc"]["count"] == 1
    res = open_findings(wd)
    assert len(res["findings"]) == 1 and res["unverified_candidates"] == [] and res["boilerplate"] == []
    f = res["findings"][0]
    assert f["source"]["doi"] == P["pmc"]["doi"] and f["source"]["license"] == "CC BY 4.0"
    assert f["source"]["found_via"] == "open:europepmc" and f["attribution"] == "unattributed"
    assert f["severity"] == "flag" and f["overlap_words"] >= 20
    assert res["sources_compared"][0]["doi"] == P["pmc"]["doi"]


def test_verify_round_carries_only_public_urls(tmp_path):
    wd = run_open(tmp_path, f"# Introduction\n\n{D1} {D3}\n")
    plan = verify_round(wd)
    plan_json = json.loads(open(plan["plan"], encoding="utf-8").read())
    assert plan_json["tier"] == "open" and plan_json["round"] == "verify"
    assert [r["carries"] for r in plan_json["requests"]] == ["public_url"]     # both hits are the same article
    assert all(r["text_sent"] is None and r["url"].endswith("/fullTextXML") for r in plan_json["requests"])
    for sentence in (D1, D3):
        assert sentence[:30].replace(" ", "%20") not in plan_json["requests"][0]["url"]
    log = [json.loads(l) for l in (wd / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["round"] for e in log] == ["search", "search", "verify"]
    assert [e["carries"] for e in log] == ["user_text", "user_text", "public_url"]
    assert verify_round(wd) == {"done": True}


def test_t1_open_run_no_finding(tmp_path):
    # "RPA" is an undefined acronym, so the sentence is excluded by default; the user re-adds it by id (c1)
    wd = run_open(tmp_path, f"# Introduction\n\n{A['miss_sentence']}\n", include="c1")     # recorded zero-hit response
    assert verify_round(wd) == {"done": True}
    res = open_findings(wd)
    assert res["findings"] == [] and res["unverified_candidates"] == [] and res["boilerplate"] == []


def test_search_hit_without_text_match_not_reported(tmp_path):
    wd = run_open(tmp_path, f"# Introduction\n\n{D1}\n")
    hit = fake_hit(full_text="Entirely different text about birds and their migration patterns across the continents "
                              "in every season of the year, with nothing in common with the query.")
    synthetic_state(wd, D1, hit)
    res = open_findings(wd)
    assert res["findings"] == [] and res["unverified_candidates"] == []


def test_snippet_only_hit_is_unverified_candidate(tmp_path):
    wd = run_open(tmp_path, f"# Introduction\n\n{D1}\n")
    synthetic_state(wd, D1, fake_hit(snippet="... {{{process mining has an advantage}}} over other ...", url="https://ol.org/x",
                                     backend="openlibrary", title="A Book"))
    res = open_findings(wd)
    assert res["findings"] == []
    [u] = res["unverified_candidates"]
    assert u["backend"] == "openlibrary" and u["title"] == "A Book" and u["url"] == "https://ol.org/x"
    assert "advantage" in u["snippet"] and u["reason"] == "snippet_only" and u["passage"] == D1


def test_unfetchable_hit_is_unverified_not_finding(tmp_path):
    wd = run_open(tmp_path, f"# Introduction\n\n{D1}\n")
    synthetic_state(wd, D1, fake_hit(fulltext_url="https://x.org/paper.pdf"))
    assert open_findings(wd)["unverified_candidates"][0]["reason"] == "not_fetched"


def test_high_hit_count_is_boilerplate(tmp_path):
    stock = P["t5"]["sentence"]
    wd = run_open(tmp_path, f"# Introduction\n\n{stock} Another remark of our own follows here to pad the text.\n")
    hit = fake_hit(full_text=f"Intro. {stock} Outro of the source text.")
    synthetic_state(wd, stock, hit, hit_count=7)
    res = open_findings(wd)
    assert res["findings"] == [] and len(res["boilerplate"]) >= 1
    assert any(b.get("overlap_words") or b.get("search_hit_count") for b in res["boilerplate"])
    synthetic_state(wd, stock, fake_hit(), hit_count=7)       # nothing verifiable, still reported as common phrasing
    res2 = open_findings(wd)
    assert res2["findings"] == [] and res2["boilerplate"][0]["search_hit_count"] == 7


def test_verified_source_reports_full_passage_not_only_sentence(tmp_path):
    first = A["passage"].split(". ")[0] + "."
    wd = run_open(tmp_path, f"# Introduction\n\nOpening words of our own here. {A['passage']} Closing words follow.\n")
    synthetic_state(wd, first, fake_hit(full_text=pdf_a_text(), doi=A["doi"], backend="openalex"))
    [f] = open_findings(wd)["findings"]
    assert f["overlap_words"] >= 40 and "However, this approach is unreliable" in f["passage"]
    assert f["source"]["found_via"] == "open:openalex"


def test_hit_matching_cited_reference_is_cited_not_quoted(tmp_path):
    md = (f"# Introduction\n\n{D1} [1]\n\n# References\n\n[1] Maruster, L.: Identifying frequent health care users. "
          f"JMIR 2021. https://doi.org/{P['pmc']['doi']}\n")
    wd = run_open(tmp_path, md)
    verify_round(wd)
    [f] = open_findings(wd)["findings"]
    assert f["attribution"] == "cited_not_quoted" and f["severity"] == "light" and f["reference_entry"] == 1


def test_verify_sentence_thresholds():
    from extract import Line

    cand = Candidate("c1", "s1", 1, 1, "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda omega", 1.0, None)
    full = [Line(1, "x y alpha beta gamma delta epsilon zeta eta theta iota kappa lambda omega z")]
    assert verify_sentence(cand, full).longest_run == 12
    seven = [Line(1, "alpha beta gamma delta epsilon zeta eta other words here")]
    assert verify_sentence(cand, seven) is None
    eight = [Line(1, "alpha beta gamma delta epsilon zeta eta theta other words here")]
    assert verify_sentence(cand, eight).longest_run == 8


def test_verified_short_overlap_is_reported_from_eight_words(tmp_path):
    sentence = ("Quasar harvesting techniques rely on gravitational lenses instead of expensive orbital mirrors "
                "according to our fictional survey of the field.")
    wd = run_open(tmp_path, f"# Introduction\n\n{sentence}\n", include="c1")
    hit = fake_hit(full_text="Other words. Quasar harvesting techniques rely on gravitational lenses instead of "
                             "something else entirely. More words.")
    synthetic_state(wd, sentence, hit)
    [f] = open_findings(wd)["findings"]
    assert f["overlap_words"] == 9 and f["source"]["found_via"] == "open:europepmc"
