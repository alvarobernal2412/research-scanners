import io
import json
import urllib.error
import urllib.parse

import prepare
import strict
from approval import STRICT_CARRIES
from conftest import FIXTURES, blank_pdf
from extract import Line
from findings import SourceRec, compare_sources, strict_findings
from helpers import call_main, drive_strict, write_md
from net import replay_opener
from textnorm import tokenize

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))
A = P["workA"]
REPLAY = replay_opener(FIXTURES / "responses")


def make_doc(tmp_path, body, refs=None, name="doc.md", extra=""):
    refs = [f"[1] {A['ref_string']}"] if refs is None else refs
    text = ("# Introduction\n\nWe study robotic process mining in this paper. "
            "Many routines are carried out by clerks.\n\n" + body + "\n\n"
            "# Method\n\nThe approach is described here with original wording of our own making.\n\n"
            + extra + "# References\n\n" + "\n".join(refs) + "\n")
    return write_md(tmp_path, name, text)


def run_pipeline(tmp_path, doc, opener=REPLAY):
    wd = tmp_path / "wd"
    code, summary = call_main(prepare.main, [str(doc), "--workdir", str(wd)])
    assert code == 0, summary
    drive_strict(wd, opener)
    return wd, summary


def test_prepare_lists_sections_and_defaults(tmp_path):
    wd, summary = run_pipeline(tmp_path, make_doc(tmp_path, A["passage"]))
    secs = {s["heading"]: s for s in summary["sections"]}
    assert secs["Introduction"]["default_open"] and not secs["Method"]["default_open"]
    assert secs["References"]["excluded"] and secs["References"]["kind"] == "references"
    assert summary["references"] == {"count": 1, "with_ids": 0, "unpublished": 0}
    assert summary["warnings"] == [] and len(summary["document_sha256"]) == 64


def test_prepare_scanned_pdf_exit_2_no_text_extracted(tmp_path):
    p = tmp_path / "scan.pdf"
    p.write_bytes(blank_pdf())
    code, out = call_main(prepare.main, [str(p), "--workdir", str(tmp_path / "wd")])
    assert code == 2 and out == {"error": "no_text_extracted"}


def test_prepare_empty_md_exit_2(tmp_path):
    code, out = call_main(prepare.main, [str(write_md(tmp_path, "e.md", "  \n")), "--workdir", str(tmp_path / "wd")])
    assert code == 2 and out["error"] == "no_text_extracted"
    code, out = call_main(prepare.main, [str(tmp_path / "missing.md")])
    assert code == 2 and out["error"] == "file_not_found"


def test_prepare_flags_glued_words_and_missing_references(tmp_path):
    glued = "Forcompleteness,Table1reproducesthefollowingresultsinfulldetailaboutthemethods " * 3
    code, out = call_main(prepare.main, [str(write_md(tmp_path, "g.md", "# Intro\n" + glued + "\n")),
                                         "--workdir", str(tmp_path / "wd")])
    assert code == 0 and set(out["warnings"]) == {"glued_words", "references_not_found"}


def test_unattributed_copy_of_cited_oa_work_flagged(tmp_path):
    wd, _ = run_pipeline(tmp_path, make_doc(tmp_path, A["passage"]))
    res = strict_findings(wd)
    assert len(res["findings"]) == 1 and res["informational"] == [] and res["boilerplate"] == []
    f = res["findings"][0]
    assert f["source"]["doi"] == A["doi"] and f["attribution"] == "unattributed" and f["severity"] == "flag"
    assert f["source"]["license"] == "cc-by" and f["source"]["version"] == "publishedVersion"
    assert f["source"]["found_via"] == "strict:reference" and f["match_type"] in ("verbatim", "near_verbatim")
    assert f["overlap_words"] >= 40 and f["confidence"] == "high"
    assert f["location"]["section"] == "s1" and f["reference_entry"] == 1
    assert f["passage"].startswith("An alternative approach")
    assert res["sources_compared"][0]["doi"] == A["doi"]


def test_cited_not_quoted_is_light(tmp_path):
    wd, _ = run_pipeline(tmp_path, make_doc(tmp_path, A["passage"] + " [1]"))
    [f] = strict_findings(wd)["findings"]
    assert f["attribution"] == "cited_not_quoted" and f["severity"] == "light"


def test_quoted_and_cited_is_informational_only(tmp_path):
    wd, _ = run_pipeline(tmp_path, make_doc(tmp_path, 'As reported, "' + A["passage"] + '" [1]'))
    res = strict_findings(wd)
    assert res["findings"] == [] and len(res["informational"]) == 1
    assert res["informational"][0]["attribution"] == "quoted_cited" and res["informational"][0]["severity"] == "info"


def test_passage_only_in_reference_list_or_footnote_no_finding(tmp_path):
    refs = [f"[1] {A['ref_string']}", f"[2] Someone: A note quoting {A['passage']}"]
    doc = make_doc(tmp_path, "Our own words only appear in this body paragraph about nothing in particular.", refs,
                   extra="Text with a note[^1].\n\n[^1]: " + A["passage"] + "\n\n")
    wd, _ = run_pipeline(tmp_path, doc)
    res = strict_findings(wd)
    assert res["findings"] == [] and res["informational"] == [] and res["boilerplate"] == []
    assert len(res["sources_compared"]) == 1       # the source was compared; the excluded text never matched


def test_t1_reference_reported_not_obtained(tmp_path):
    ref = f"[1] {P['t1']['ref_string']} https://doi.org/{P['t1']['doi']}"
    wd, summary = run_pipeline(tmp_path, make_doc(tmp_path, "Plain original sentence about something else entirely.",
                                                  [ref]))
    assert summary["references"]["with_ids"] == 1
    state = json.loads((wd / "state.json").read_text(encoding="utf-8"))
    [src] = state["sources"]
    assert src["doi"] == P["t1"]["doi"] and src["status"] == "not_obtained"
    assert src["reason"] == "only_supplementary_files"
    assert strict_findings(wd)["findings"] == []


def test_boilerplate_three_sources_not_flagged(tmp_path):
    stock = ("Process mining techniques extract valuable knowledge from event logs recorded by modern "
             "information systems in organisations")
    doc = make_doc(tmp_path, stock + ".")
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(doc), "--workdir", str(wd)])
    recs = [SourceRec(id=f"s{i}", lines=[Line(1, f"Intro words number {i} here. {stock}. More filler text {i} follows.")],
                      doi=f"10.1/{i}") for i in range(3)]
    res = compare_sources(wd, recs)
    assert res["findings"] == [] and len(res["boilerplate"]) == 3
    res2 = compare_sources(wd, recs[:2])
    assert len(res2["findings"]) == 2 and res2["boilerplate"] == []


def test_original_sentence_no_finding(tmp_path):
    wd, _ = run_pipeline(tmp_path, make_doc(tmp_path, "Our pipeline stitches together heterogeneous clickstreams with "
                                                      "a lightweight clustering step tuned on invented data."))
    assert strict_findings(wd)["findings"] == []


def test_strict_requests_carry_only_public_identifiers(tmp_path):
    body = A["passage"] + " " + P["t2_standin"]["sentence"]
    wd, _ = run_pipeline(tmp_path, make_doc(tmp_path, body))
    log = [json.loads(l) for l in (wd / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log and all(e["carries"] in STRICT_CARRIES for e in log) and all(e["text_sent"] is None for e in log)
    body_keys = [t.key for t in tokenize([Line(1, body)])]
    windows = {" ".join(body_keys[i:i + 5]) for i in range(len(body_keys) - 4)}
    for e in log:
        url_keys = [t.key for t in tokenize([Line(1, urllib.parse.unquote(e["url"]).replace("+", " "))])]
        url_windows = {" ".join(url_keys[i:i + 5]) for i in range(len(url_keys) - 4)}
        assert not windows & url_windows, e["url"]
        assert "tidy tables" not in urllib.parse.unquote(e["url"])
    assert {e["round"] for e in log} == {"resolve", "locate", "download"}


def test_run_with_wrong_hash_exit_2_sends_nothing(tmp_path):
    doc = make_doc(tmp_path, A["passage"])
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(doc), "--workdir", str(wd)])
    _, plan = call_main(strict.main, ["plan", "--workdir", str(wd)])

    def boom(*a, **k):
        raise AssertionError("network must not be touched")

    code, out = call_main(strict.main, ["run", "--workdir", str(wd), "--plan", plan["plan"], "--sha256", "0" * 64],
                          opener=boom)
    assert code == 2 and out["error"] == "plan_hash_mismatch"
    assert not (wd / "requests.jsonl").exists()


def test_run_rejects_plan_for_other_document(tmp_path):
    wd1, wd2 = tmp_path / "w1", tmp_path / "w2"
    call_main(prepare.main, [str(make_doc(tmp_path, A["passage"])), "--workdir", str(wd1)])
    call_main(prepare.main, [str(make_doc(tmp_path, "Different body text entirely.", name="b.md")),
                             "--workdir", str(wd2)])
    _, plan = call_main(strict.main, ["plan", "--workdir", str(wd1)])

    def boom(*a, **k):
        raise AssertionError("network must not be touched")

    code, out = call_main(strict.main, ["run", "--workdir", str(wd2), "--plan", plan["plan"],
                                        "--sha256", plan["sha256"]], opener=boom)
    assert code == 2 and out["error"] == "plan_document_mismatch"


def test_missing_workdir_and_plan(tmp_path):
    code, out = call_main(strict.main, ["plan", "--workdir", str(tmp_path / "none")])
    assert code == 2 and out["error"] == "workdir_missing"
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(make_doc(tmp_path, A["passage"])), "--workdir", str(wd)])
    code, out = call_main(strict.main, ["run", "--workdir", str(wd), "--plan", str(tmp_path / "nope.json"),
                                        "--sha256", "0" * 64])
    assert code == 2 and out["error"] == "plan_missing"


def test_plan_done_when_nothing_left(tmp_path):
    wd, _ = run_pipeline(tmp_path, make_doc(tmp_path, A["passage"]))
    code, out = call_main(strict.main, ["plan", "--workdir", str(wd)])
    assert out == {"done": True}


def test_backend_errors_are_recorded_not_hidden(tmp_path):
    def failing(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b""))

    wd, _ = run_pipeline(tmp_path, make_doc(tmp_path, A["passage"]), opener=failing)
    state = json.loads((wd / "state.json").read_text(encoding="utf-8"))
    assert state["references"][0]["resolution"] == "backend_error"
    assert strict_findings(wd)["findings"] == []
