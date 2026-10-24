import io
import json
import re
import urllib.error

import acquire
import open_tier
import prepare
import report
from conftest import FIXTURES, make_pdf
from helpers import call_main, drive_strict, write_md
from net import replay_opener
from open_tier import Candidate  # noqa: F401
from test_verify import verify_round

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))
A = P["workA"]
D1 = P["pmc"]["sentences"][0]
REPLAY = replay_opener(FIXTURES / "responses")
BANNED = ("clean", "infring", "probability", "plagiarised", "plagiarized", "guilty")


class Web:
    """Synthetic web for DOIs 10.9999/synN: OpenAlex work records and their PDFs (no recording needed)."""

    def __init__(self, n=3, fail=None):
        self.n, self.fail, self.calls = n, fail, []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        if self.fail:
            raise urllib.error.HTTPError(url, self.fail, "x", {}, io.BytesIO(b""))
        m = re.search(r"works/doi:10\.9999/syn(\d+)", url)
        if m:
            k = m.group(1)
            body = json.dumps({"title": f"Synthetic paper {k}", "open_access": {"is_oa": True, "oa_status": "gold"},
                               "best_oa_location": {"pdf_url": f"https://repo.example.org/syn{k}.pdf", "is_oa": True,
                                                    "license": "cc-by", "version": "publishedVersion"},
                               "locations": []}).encode()
            return Resp(body)
        m = re.search(r"syn(\d+)\.pdf", url)
        if m:
            return Resp(make_pdf([f"Synthetic source number {m.group(1)} talks about quasar harvesting techniques."]))
        raise urllib.error.URLError("not recorded")


class Resp(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.status, self.headers = 200, {}

    def geturl(self):
        return None


def refs_block(n):
    return "\n".join(f"[{i}] Author{i}, A.: Synthetic paper {i}. https://doi.org/10.9999/syn{i}" for i in range(1, n + 1))


def make_run(tmp_path, body="Our own words about nothing in particular appear in this paragraph.", refs=None,
             opener=None, name="doc.md"):
    refs = refs_block(3) if refs is None else refs
    md = f"# Introduction\n\n{body}\n\n# References\n\n{refs}\n"
    wd = tmp_path / "wd"
    code, out = call_main(prepare.main, [str(write_md(tmp_path, name, md)), "--workdir", str(wd)])
    assert code == 0, out
    if opener is not None:
        drive_strict(wd, opener)
    return wd


def rep(wd):
    return report.build_report(wd)


def test_coverage_is_first_key(tmp_path):
    r = rep(make_run(tmp_path, opener=Web()))
    assert list(r)[:3] == ["coverage", "summary", "findings"]
    assert list(r) == ["coverage", "summary", "findings", "informational", "boilerplate", "unverified_candidates",
                       "disclosure", "disclaimer"]
    assert r["disclaimer"] == report.DISCLAIMER


def test_no_network_rounds_gives_insufficient_coverage(tmp_path):
    r = rep(make_run(tmp_path))                       # only prepare ran
    c = r["coverage"]
    assert c["status"] == "insufficient_coverage" and c["tiers_used"] == [] and r["disclosure"]["requests_sent"] == []
    assert c["references"]["total"] == 3 and len(c["references"]["full_text_not_obtained"]) == 3
    assert {x["reason"] for x in c["references"]["full_text_not_obtained"]} == {"not_attempted"}
    assert c["status_reasons"]


def test_small_checked_corpus_never_no_overlap_status(tmp_path):
    refs = "\n".join([f"[1] {A['ref_string']} https://doi.org/{A['doi']}"] +
                     [f"[{i}] Someone{i}, S.: Work in preparation number {i}, unpublished manuscript." for i in range(2, 11)])
    wd = make_run(tmp_path, refs=refs, opener=REPLAY)
    c = rep(wd)["coverage"]
    assert c["references"]["total"] == 10 and len(c["references"]["full_text_obtained"]) == 1
    assert c["status"] == "insufficient_coverage"
    assert len(c["references"]["unresolved"]) == 9 and c["references"]["unresolved"][0]["resolution"] == "not_sent_unpublished"


def test_enough_corpus_gives_no_overlap_status_with_limits(tmp_path):
    wd = make_run(tmp_path, opener=Web(3))
    r = rep(wd)
    c = r["coverage"]
    assert c["status"] == "no_overlap_found_in_checked_sources" and r["findings"] == []
    assert len(c["references"]["full_text_obtained"]) == 3 and c["references"]["checked_of_total"] == {"checked": 3, "total": 3}
    assert c["tiers_used"] == ["strict"]
    for line in report.LIMITS_ALWAYS:
        assert line in c["limits"]
    assert any("Paywalled" in l for l in c["limits"]) and any("Unpublished" in l for l in c["limits"])
    assert c["references"]["full_text_obtained"][0]["license"] == "cc-by"


def test_findings_status_when_overlap_found(tmp_path):
    wd = make_run(tmp_path, body=A["passage"], refs=f"[1] {A['ref_string']}", opener=REPLAY)
    r = rep(wd)
    assert r["coverage"]["status"] == "findings" and r["summary"]["findings_by_severity"] == {"flag": 1, "light": 0}
    assert r["findings"][0]["id"] == "f1" and r["summary"]["sources_compared"] == 1


def test_backend_errors_listed_and_force_insufficient(tmp_path):
    wd = make_run(tmp_path, opener=Web(3, fail=500))
    r = rep(wd)
    c = r["coverage"]
    assert c["status"] == "insufficient_coverage"
    assert {x["reason"] for x in c["references"]["full_text_not_obtained"]} == {"backend_error"}
    assert c["backends"][0]["service"] == "openalex" and c["backends"][0]["errors"] == {"http_5xx": 3}
    assert any("failed" in reason for reason in c["status_reasons"])


def test_glued_document_forces_insufficient(tmp_path):
    glued = "Forcompleteness,Table1reproducesthefollowingresultsinfulldetailaboutthemethods " * 3
    wd = make_run(tmp_path, body=glued, opener=Web(3))
    c = rep(wd)["coverage"]
    assert "glued_words" in c["extraction"]["warnings"] and c["status"] == "insufficient_coverage"
    assert any("glued" in r for r in c["status_reasons"])


def test_unresolved_references_listed_with_text(tmp_path):
    refs = (f"[1] {A['ref_string']}\n[2] {P['book_ref']['ref_string']}\n"
            "[3] Doe, J.: A forthcoming chapter on things, in preparation.")
    wd = make_run(tmp_path, refs=refs, opener=REPLAY)
    c = rep(wd)["coverage"]["references"]
    by_n = {u["n"]: u for u in c["unresolved"]}
    assert by_n[2]["resolution"] == "crossref_low_score" and "Tukey" in by_n[2]["text"]
    assert by_n[3]["resolution"] == "not_sent_unpublished" and "forthcoming" in by_n[3]["text"]
    assert c["resolved"] == 1 and c["total"] == 3


def test_t1_case_shows_not_obtained_and_limits(tmp_path):
    refs = f"[1] {P['t1']['ref_string']} https://doi.org/{P['t1']['doi']}"
    wd = make_run(tmp_path, body=P["t1"]["sentence"], refs=refs, opener=REPLAY)
    r = rep(wd)
    [n] = r["coverage"]["references"]["full_text_not_obtained"]
    assert n["doi"] == P["t1"]["doi"] and n["reason"] == "only_supplementary_files"
    assert r["coverage"]["status"] == "insufficient_coverage" and r["findings"] == []
    limits = " ".join(r["coverage"]["limits"])
    assert "Paywalled" in limits and "Unpublished" in limits and "Paraphrase" in limits


def test_over_cap_counts_reported(tmp_path):
    def sentence(tag):
        return ("Qwertyuiop" + tag + " zxcvbnmasd lkjhgfdsa pqowieurty mnbvcxzlkj hgfdsapoiu ytrewqlkjh gfdsamnbvc "
                "xzlkjhpoiu ytrewqazxs wedcrfvtgb yhnujmikol.")
    parts = []
    for h, base in (("Introduction", "a"), ("Background", "b"), ("Related Work", "c")):
        parts.append(f"# {h}\n\n" + " ".join(sentence(base * (k + 1)) for k in range(10)) + "\n")
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", "\n".join(parts))), "--workdir", str(wd)])
    code, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1,s2,s3",
                                            "--backends", "europepmc"])
    assert code == 0 and sum(plan["over_cap"].values()) == 20
    o = rep(wd)["coverage"]["open"]
    assert sum(o["not_queried_over_cap"].values()) == 20 and o["sentences_eligible"] == 30
    assert o["sections_selected"] == ["s1", "s2", "s3"] and o["sentences_queried"] == 0


def test_open_tier_in_report_and_disclosure_lists_every_request(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_PLAGIARISM_MAILTO", "me@example.org")
    wd = make_run(tmp_path, body=D1, refs=f"[1] {A['ref_string']}", opener=REPLAY)
    code, plan = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s1", "--backends", "europepmc"])
    code, res = call_main(open_tier.main, ["run", "--workdir", str(wd), "--plan", plan["plan"],
                                           "--sha256", plan["sha256"]], opener=REPLAY)
    verify_round(wd)
    r = rep(wd)
    log = [json.loads(l) for l in (wd / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    sent = r["disclosure"]["requests_sent"]
    assert len(sent) == len(log) and len(sent) >= 5
    assert r["coverage"]["tiers_used"] == ["strict", "open"] == r["disclosure"]["tiers_used"]
    assert "TEXT_PLAGIARISM_MAILTO" not in json.dumps(r["disclosure"]["requests_sent"]) and "me@example.org" not in json.dumps(r)
    assert r["disclosure"]["identities_used"] == ["TEXT_PLAGIARISM_MAILTO"]
    assert {"crossref", "openalex", "europepmc"} <= set(r["disclosure"]["services_contacted"])
    texts = [s["text_sent"] for s in sent if s["text_sent"]]
    assert texts == [D1]
    assert {s["tier"] for s in sent} == {"strict", "open"}
    [f] = r["findings"]
    assert f["source"]["found_via"] == "open:europepmc" and r["coverage"]["open"]["sentences_queried"] == 1
    assert r["coverage"]["status"] == "findings"
    assert any("Europe PMC" in l for l in r["coverage"]["limits"])


def test_report_never_contains_banned_words(tmp_path):
    wd = make_run(tmp_path, body=A["passage"], refs=f"[1] {A['ref_string']}", opener=REPLAY)
    other = tmp_path / "other"
    other.mkdir()
    for variant in (rep(wd), rep(make_run(other))):
        text = json.dumps(variant).lower()
        for word in BANNED:
            assert word not in text, word
    for status in ("findings", "no_overlap_found_in_checked_sources", "insufficient_coverage"):
        assert all(w not in status for w in BANNED)
    assert not any(w in report.DISCLAIMER.lower() for w in BANNED)
    assert not any(w in " ".join(report.LIMITS_ALWAYS + list(report.LIMITS_OPEN.values()) + [report.LIMIT_CORPUS]).lower()
                   for w in BANNED)


def test_cli_missing_workdir_exit_2(tmp_path):
    code, out = call_main(report.main, ["--workdir", str(tmp_path / "none")])
    assert code == 2 and out == {"error": "workdir_missing", "detail": str(tmp_path / "none")}
    wd = make_run(tmp_path, opener=Web(3))
    code, out = call_main(report.main, ["--workdir", str(wd)])
    assert code == 0 and list(out)[0] == "coverage"


def test_corpus_items_listed_and_counted_in_coverage(tmp_path):
    src = tmp_path / "papers"
    src.mkdir()
    for k in (1, 2, 3):
        (src / f"p{k}.txt").write_text(f"Corpus paper {k} discusses zeppelin logistics and harbour scheduling in depth.", "utf-8")
    md = f"# Introduction\n\nOur own words here.\n"
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", md)), "--workdir", str(wd), "--corpus", str(src)])
    r = rep(wd)
    c = r["coverage"]
    assert c["corpus"]["compared"] == 3 and len(c["corpus"]["provided"]) == 3 and c["corpus"]["not_loaded"] == []
    assert c["status"] == "no_overlap_found_in_checked_sources" and c["references"]["checked_of_total"] == {"checked": 3, "total": 3}
    assert report.LIMIT_CORPUS in c["limits"] and r["summary"]["corpus_findings"] == 0


def test_unloaded_corpus_item_forces_listing_and_cannot_hide_behind_clean_status(tmp_path):
    src = tmp_path / "papers"
    src.mkdir()
    for k in (1, 2, 3):
        (src / f"p{k}.txt").write_text(f"Corpus paper {k} discusses zeppelin logistics and harbour scheduling in depth.", "utf-8")
    (src / "broken.pdf").write_bytes(b"not a pdf")
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", "# Introduction\n\nOur own words here.\n")),
                             "--workdir", str(wd), "--corpus", str(src)])
    c = rep(wd)["coverage"]
    assert c["corpus"]["compared"] == 3
    assert c["corpus"]["not_loaded"] == [{"id": "c1", "name": "broken.pdf", "status": "unreadable", "reason": "unreadable_file"}]
    assert c["status"] == "insufficient_coverage" and any("corpus item" in r for r in c["status_reasons"])


UNPUB = [f"[{i}] Someone{i}, S.: Work in preparation number {i}, unpublished manuscript." for i in range(4, 7)]


def test_partial_coverage_is_stated_even_with_findings(tmp_path):
    refs = f"[1] {A['ref_string']}\n[2] Doe, J.: A forthcoming chapter on things, in preparation."
    wd = make_run(tmp_path, body=A["passage"], refs=refs, opener=REPLAY)
    c = rep(wd)["coverage"]
    assert c["status"] == "findings"
    assert any("only 1 of 2" in r for r in c["status_reasons"])


def test_half_of_cited_works_unchecked_is_not_no_overlap(tmp_path):
    refs = refs_block(3) + "\n" + "\n".join(UNPUB)
    c = rep(make_run(tmp_path, refs=refs, opener=Web(3)))["coverage"]
    assert c["references"]["checked_of_total"] == {"checked": 3, "total": 6}
    assert c["status"] == "insufficient_coverage"
    assert any("only 3 of 6" in r for r in c["status_reasons"])


class PaywallWeb(Web):
    def __call__(self, request, timeout=None):
        if "syn4" in request.full_url and "works/doi" in request.full_url:
            return Resp(json.dumps({"title": "Closed", "open_access": {"is_oa": False}, "best_oa_location": None,
                                    "locations": []}).encode())
        return super().__call__(request, timeout)


def test_known_paywalled_work_does_not_block_no_overlap_but_is_stated(tmp_path):
    c = rep(make_run(tmp_path, refs=refs_block(4), opener=PaywallWeb(4)))["coverage"]
    assert c["references"]["full_text_not_obtained"][0]["reason"] == "paywalled"
    assert c["status"] == "no_overlap_found_in_checked_sources"
    assert any("only 3 of 4" in r for r in c["status_reasons"])


def _fake_open_state(wd, n_queried, over_cap):
    cands, _ = open_tier.all_candidates(wd)
    cands = [c for c in cands if c.excluded is None][:n_queried]
    from dataclasses import asdict
    state = {"version": 1, "use_keys": False,
             "selection": {"sections": ["s1"], "backends": ["europepmc"], "cap": 10,
                           "candidates": [asdict(c) for c in cands], "excluded": [], "over_cap": over_cap,
                           "backend_over_cap": {}},
             "queries": {f"0:r{i:03d}": {"candidate": c.id, "backend": "europepmc", "text": c.text, "req_id": f"r{i:03d}"}
                         for i, c in enumerate(cands, 1)},
             "results": {f"0:r{i:03d}": {"hit_count": 0, "hits": [], "error": None} for i, _ in enumerate(cands, 1)},
             "rounds": [{"round": "search"}]}
    (wd / "open_state.json").write_text(json.dumps(state), encoding="utf-8")


def _long_body():
    return " ".join(f"Qwerty{chr(97 + k)}x zxcvbnmasd lkjhgfdsa pqowieurty mnbvcxzlkj hgfdsapoiu ytrewqlkjh "
                    "gfdsamnbvc xzlkjhpoiu ytrewqazxs wedcrfvtgb yhnujmikol." for k in range(12))


def test_open_tier_alone_with_unqueried_sentences_is_not_no_overlap(tmp_path):
    wd = make_run(tmp_path, body=_long_body(), refs="")
    _fake_open_state(wd, 10, {"s1": 2})
    c = rep(wd)["coverage"]
    assert c["status"] == "insufficient_coverage" and any("not queried" in r for r in c["status_reasons"])
    wd2 = make_run(tmp_path / "ok", body=_long_body(), refs="") if (tmp_path / "ok").mkdir() is None else None
    _fake_open_state(wd2, 10, {})
    c2 = rep(wd2)["coverage"]
    assert c2["status"] == "no_overlap_found_in_checked_sources", (c2["status_reasons"], c2["open"])


def test_open_tier_alone_with_unchecked_references_is_not_no_overlap(tmp_path):
    wd = make_run(tmp_path, body=_long_body(), refs=refs_block(2))      # never fetched
    _fake_open_state(wd, 10, {})
    c = rep(wd)["coverage"]
    assert c["status"] == "insufficient_coverage"
