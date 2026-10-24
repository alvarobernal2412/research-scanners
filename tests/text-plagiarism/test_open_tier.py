import json
import urllib.parse

import open_tier
import prepare
from conftest import FIXTURES
from helpers import call_main, write_md
from open_tier import (OPEN_QUERY_CAP, distinctiveness, phrase_for_query, select_sentences)

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))

ELIGIBLE = ("Qwertyuiop zxcvbnmasd lkjhgfdsa pqowieurty mnbvcxzlkj hgfdsapoiu ytrewqlkjh gfdsamnbvc xzlkjhpoiu "
            "ytrewqazxs wedcrfvtgb yhnujmikol")


def sentence(tag):
    letters = "".join(chr(97 + int(d)) for d in str(tag) if d.isdigit()) + "".join(c for c in str(tag) if c.isalpha())
    return ELIGIBLE.replace("Qwertyuiop", "Qwertyuiop" + letters) + "."


def prep(tmp_path, md):
    wd = tmp_path / "wd"
    code, out = call_main(prepare.main, [str(write_md(tmp_path, "d.md", md)), "--workdir", str(wd)])
    assert code == 0, out
    return wd, {s["heading"]: s["id"] for s in out["sections"]}


def test_only_selected_sections_are_candidates(tmp_path):
    wd, ids = prep(tmp_path, f"# Introduction\n\n{sentence('a')} {sentence('b')}\n\n# Method\n\n{sentence('c')}\n")
    sel, exc, over = select_sentences(wd, [ids["Introduction"]], set())
    assert {c.section_id for c in sel} == {ids["Introduction"]} and len(sel) == 2 and not exc and not over


def test_excluded_sections_never_candidates_even_if_selected(tmp_path):
    md = (f"# Introduction\n\n{sentence('a')}\n\n# Acknowledgements\n\n{sentence('b')}\n\n"
          f"# References\n\n[1] {sentence('c')}\n")
    wd, ids = prep(tmp_path, md)
    sel, exc, over = select_sentences(wd, list(ids.values()), set())
    assert [c.section_id for c in sel] == [ids["Introduction"]] and not exc


def test_numbers_equations_novel_terms_excluded(tmp_path):
    md = ("# Introduction\n\n"
          "In 2020 we collected the many traces from several organisations that run these complicated pipelines daily.\n\n"
          "Given the constraint x = y + z we can derive the following bound on the number of steps required here.\n\n"
          "The Event Process Mining Browser (EPMB) lets analysts inspect traces and navigate large event logs quickly.\n\n"
          "The proposed language, ProvGraph, captures events and their relations in a uniform and simple way again.\n\n"
          "We call the resulting structure a lineage ribbon because it keeps the history of every single change.\n\n"
          f"{sentence('ok')}\n")
    wd, ids = prep(tmp_path, md)
    sel, exc, _ = select_sentences(wd, [ids["Introduction"]], set())
    assert len(sel) == 1 and "Qwertyuiop" in sel[0].text
    assert sorted(c.excluded for c in exc) == ["equation", "novel_term", "novel_term", "novel_term", "numbers"]


def test_quoted_sentences_excluded(tmp_path):
    wd, ids = prep(tmp_path, f'# Introduction\n\n"{sentence("q")[:-1]}."\n')
    sel, exc, _ = select_sentences(wd, [ids["Introduction"]], set())
    assert not sel and [c.excluded for c in exc] == ["quoted"]


def test_include_readds_excluded_by_id(tmp_path):
    md = "# Introduction\n\nIn 2020 we collected the many traces from several organisations that run these complicated pipelines.\n"
    wd, ids = prep(tmp_path, md)
    sel, exc, _ = select_sentences(wd, [ids["Introduction"]], set())
    assert not sel and exc[0].excluded == "numbers"
    sel2, exc2, _ = select_sentences(wd, [ids["Introduction"]], {exc[0].id})
    assert [c.id for c in sel2] == [exc[0].id] and not exc2


def test_length_bounds(tmp_path):
    short = "Word " + " ".join(["word"] * 10) + "."
    ok12 = "Alphabet " + " ".join(["alphabet"] * 11) + "."
    ok40 = "Alphabet " + " ".join(["alphabet"] * 39) + "."
    long41 = "Alphabet " + " ".join(["alphabet"] * 40) + "."
    wd, ids = prep(tmp_path, f"# Introduction\n\n{short} {ok12} {ok40} {long41}\n")
    sel, exc, _ = select_sentences(wd, [ids["Introduction"]], set())
    assert len(sel) == 2 and sorted(c.excluded for c in exc) == ["too_long", "too_short"]


def test_cap_round_robin_across_sections_and_over_cap_counts(tmp_path):
    parts = []
    n = 0
    for h in ("Introduction", "Background", "Related Work"):
        sents = []
        for _ in range(10):
            n += 1
            sents.append(sentence(f"x{n}"))
        parts.append(f"# {h}\n\n" + " ".join(sents) + "\n")
    wd, ids = prep(tmp_path, "\n".join(parts))
    sel, exc, over = select_sentences(wd, list(ids.values()), set(), cap=OPEN_QUERY_CAP)
    assert len(sel) == 10 and len(over) == 20
    assert {c.section_id for c in sel} == set(ids.values())
    assert sorted(sum(1 for c in sel if c.section_id == s) for s in ids.values()) == [3, 3, 4]


def test_priority_by_distinctiveness():
    assert distinctiveness("the approach of the model results".split()) == 0.0
    assert distinctiveness("the xylophonic quarkonium resonances".split()) == 1.0
    assert distinctiveness([]) == 0.0


def test_phrase_strips_quotes_brackets_colons():
    text = 'He said: "the (very) [odd] sentence" works {well} [1, 2] and (Smith, 2020) fine.'
    assert phrase_for_query(text) == "He said the very odd sentence works well and fine."


def test_plan_contains_exactly_selected_phrases(tmp_path):
    md = f"# Introduction\n\n{P['pmc']['sentences'][0]} {P['pmc']['sentences'][1]}\n\nIn 2020 numbers are excluded from this one anyway.\n"
    wd, ids = prep(tmp_path, md)
    code, out = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", ids["Introduction"], "--backends", "europepmc"])
    assert code == 0 and out["round"] == "search"
    plan = json.loads(open(out["plan"], encoding="utf-8").read())
    sent = sorted(r["text_sent"] for r in plan["requests"])
    assert sent == sorted(P["pmc"]["sentences"])
    assert all(r["carries"] == "user_text" and r["service"] == "europepmc" for r in plan["requests"])
    for r in plan["requests"]:
        assert r["text_sent"].replace(",", "%2C") in urllib.parse.unquote(r["url"]).replace(",", "%2C")
    svc = out["preview"]["services"]["europepmc"]
    assert svc["count"] == 2 and sorted(i["sends"] for i in svc["items"]) == sent
    assert [s["text"] for s in out["selected"]] and out["over_cap"] == {}


def test_no_identity_on_keyless_backend_without_use_keys(tmp_path, monkeypatch):
    for k in ("OPENALEX_API_KEY", "CORE_API_KEY", "TEXT_PLAGIARISM_MAILTO"):
        monkeypatch.setenv(k, "secret-value-123")
    wd, ids = prep(tmp_path, f"# Introduction\n\n{P['pmc']['sentences'][0]}\n")
    code, out = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", ids["Introduction"], "--use-keys",
                                           "--backends", "europepmc"])
    text = open(out["plan"], encoding="utf-8").read()
    assert code == 0 and "secret-value-123" not in text      # europepmc has no keyed route at all
    plan = json.loads(text)
    assert all(r["identity"] is None and "{" not in r["url"] for r in plan["requests"])


def test_plan_errors(tmp_path):
    wd, ids = prep(tmp_path, f"# Introduction\n\n{sentence('a')}\n# References\n\n[1] x\n")
    code, out = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", "s99"])
    assert code == 2 and out["error"] == "unknown_section"
    code, out = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", ids["Introduction"],
                                           "--backends", "nonsense"])
    assert code == 2 and out["error"] == "unknown_backend"
    code, out = call_main(open_tier.main, ["plan", "--workdir", str(wd), "--sections", ids["References"]])
    assert code == 2 and out["error"] == "nothing_to_plan"
    code, out = call_main(open_tier.main, ["plan", "--workdir", str(tmp_path / "none")])
    assert code == 2 and out["error"] == "workdir_missing"
