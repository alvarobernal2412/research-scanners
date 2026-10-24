"""Regressions from the whole-branch review: heading merges, verification thresholds, boilerplate spans."""
import json
from dataclasses import asdict

import open_tier
import prepare
from extract import Line
from findings import SourceRec, compare_sources
from helpers import call_main, write_md
from verify import open_findings

P1 = "recently quantum entanglement distributes correlated photon states between remote superconducting resonators reliably"
P2 = "notably mitochondrial membrane potential regulates calcium buffering inside cardiac myocytes during ischaemic stress"


def _prep(tmp_path, md):
    wd = tmp_path / "wd"
    code, out = call_main(prepare.main, [str(write_md(tmp_path, "d.md", md)), "--workdir", str(wd)])
    assert code == 0, out
    return wd


def _rec(text):
    return SourceRec(id="s", lines=[Line(1, text)], doi="10.1/s", title="S")


def test_near_verbatim_does_not_merge_across_section_heading(tmp_path):
    wd = _prep(tmp_path, f"# Introduction\n\n{P1}\n\n# Methods\n\n{P2}\n")
    res = compare_sources(wd, [_rec(f"{P1} zzz {P2}")])
    assert len(res["findings"]) == 2
    assert all(f["match_type"] == "verbatim" for f in res["findings"])


# ------------------------------------------------------------- verification thresholds

def _open_state(wd, cands, hits_by_cand, texts_hit_count=None, backend="europepmc", source_text=""):
    state = {"version": 1, "use_keys": False,
             "selection": {"sections": ["s1"], "backends": [backend], "cap": 10,
                           "candidates": [asdict(c) for c in cands], "excluded": [], "over_cap": {},
                           "backend_over_cap": {}},
             "queries": {}, "results": {}, "rounds": [{"round": "search"}]}
    for i, c in enumerate(cands, 1):
        key = f"0:r{i:03d}"
        state["queries"][key] = {"candidate": c.id, "backend": backend, "text": c.text, "req_id": f"r{i:03d}"}
        hit = {"query_id": c.id, "backend": backend, "doi": hits_by_cand[c.id]["doi"], "title": "T", "url": None,
               "license": "CC BY", "pmcid": None, "fulltext_url": None,
               "full_text": hits_by_cand[c.id].get("full_text"), "snippet": None, "notes": []}
        state["results"][key] = {"hit_count": (texts_hit_count or {}).get(c.id, 1), "hits": [hit], "error": None}
    (wd / "open_state.json").write_text(json.dumps(state), encoding="utf-8")


def _cands(wd):
    cands, _ = open_tier.all_candidates(wd)
    return [c for c in cands if c.excluded is None]


STOCK = "In this paper we present a novel approach to the analysis of event data streams in practice."
COPIED = "Process mining has an advantage over other mapping techniques as it uses factual observations retrieved from data."


def test_stock_sentence_does_not_hide_verified_copy_in_same_paragraph(tmp_path):
    wd = _prep(tmp_path, f"# Introduction\n\n{STOCK} {COPIED}\n\n# Method\n\nOriginal words of our own for the method section here.\n")
    stock, copied = _cands(wd)[:2]
    src = f"Intro text. {COPIED} More text follows here."
    _open_state(wd, [stock, copied], {stock.id: {"doi": "10.1/x"}, copied.id: {"doi": "10.2/y", "full_text": src}},
                texts_hit_count={stock.id: 40})
    res = open_findings(wd)
    assert [f["source"]["doi"] for f in res["findings"]] == ["10.2/y"]
    assert any(b.get("search_hit_count") == 40 for b in res["boilerplate"])
    assert not any("Process mining has an advantage" in b["passage"] for b in res["boilerplate"])


def test_high_hit_sentence_itself_is_still_boilerplate(tmp_path):
    wd = _prep(tmp_path, f"# Introduction\n\n{COPIED} Another remark of our own follows here to pad it.\n")
    [c] = [c for c in _cands(wd) if "Process mining" in c.text]
    _open_state(wd, [c], {c.id: {"doi": "10.2/y", "full_text": f"Intro. {COPIED} Outro."}}, texts_hit_count={c.id: 40})
    res = open_findings(wd)
    assert res["findings"] == [] and res["boilerplate"]


def test_openalex_hit_count_is_not_an_independent_document_count(tmp_path):
    wd = _prep(tmp_path, f"# Introduction\n\n{COPIED} Another remark of our own follows here to pad it.\n")
    [c] = [c for c in _cands(wd) if "Process mining" in c.text]
    _open_state(wd, [c], {c.id: {"doi": "10.2/y", "full_text": f"Intro. {COPIED} Outro."}}, backend="openalex",
                texts_hit_count={c.id: 4000})
    res = open_findings(wd)
    assert len(res["findings"]) == 1 and res["boilerplate"] == []


def test_eight_word_threshold_only_near_the_verified_sentence(tmp_path):
    other = "Overall we found that the proposed approach improves the quality of discovered process models for hospital wards."
    wd = _prep(tmp_path, f"# Introduction\n\n{COPIED}\n\n{other}\n")
    copied = next(c for c in _cands(wd) if "Process mining" in c.text)
    src = (f"{COPIED} Unrelated filler words go here to separate. In general the proposed approach improves the "
           "quality of discovered process models in many settings.")
    _open_state(wd, [copied], {copied.id: {"doi": "10.2/y", "full_text": src}})
    res = open_findings(wd)
    assert len(res["findings"]) == 1 and "Process mining" in res["findings"][0]["passage"]
