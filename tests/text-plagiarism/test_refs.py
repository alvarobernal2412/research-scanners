import json
import urllib.parse

from approval import Gate, build_plan
from conftest import FIXTURES
from extract import extract_bytes
from net import Response, replay_opener, send
from refs import RefEntry, apply_resolution, parse_references, plan_resolution
from sections import detect_sections

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))


def parse(text, fmt="txt"):
    doc = extract_bytes(text.encode("utf-8"), fmt)
    return parse_references(doc, detect_sections(doc))


def test_split_numbered_bracket_and_dot_styles():
    br = parse("1 Intro\nbody.\nReferences\n[1] Smith, J.: A title. Journal 1, 2 (2020)\n   continued line here.\n"
               "[2] Lee, K.: Other. 2019.\n")
    assert [(e.n, e.first_author, e.year) for e in br] == [(1, "Smith", "2020"), (2, "Lee", "2019")]
    assert "continued line here" in br[0].text and br[0].line_start == 4 and br[0].line_end == 5
    dot = parse("1 Intro\nbody.\nReferences\n1. Smith, J.: A title. 2020.\n2. Lee, K.: Other. 2019.\n")
    assert [e.n for e in dot] == [1, 2]


def test_split_md_list_bibitem_and_author_year_blocks():
    md = parse("# Intro\nx\n# References\n- Smith, J. (2020). A title. Journal.\n- Lee, K. (2019). Other. Venue.\n", "md")
    assert [(e.first_author, e.year, e.n) for e in md] == [("Smith", "2020", None), ("Lee", "2019", None)]
    tex = parse("\\section{A}\nx\n\\begin{thebibliography}{9}\n\\bibitem{smith20} Smith, J.: Title. 2020.\n"
                "\\bibitem{lee19} Lee, K.: Other. 2019.\n\\end{thebibliography}\n", "tex")
    assert [e.tex_key for e in tex] == ["smith20", "lee19"]
    assert tex[0].keys() >= {"tex:smith20", "ay:smith:2020"}
    blocks = parse("1 Intro\nx\nReferences\nSmith, J. (2020). A long title that wraps\nonto a second line. Journal.\n\n"
                   "Lee, K. (2019). Other. Venue.\n")
    assert len(blocks) == 2 and blocks[0].line_end == 5
    nospace = parse("1 Intro\nx\nReferences\nSmith, J. (2020). A title. Journal 1.\nLee, K. (2019). Other. Venue.\n")
    assert [e.first_author for e in nospace] == ["Smith", "Lee"]


def test_page_header_line_inside_entry_does_not_split():
    ents = parse("1 Intro\nx\nReferences\n[1] Van Suetendael, J., Depaire, B.: Understanding the behavior\n"
                 "598 J. Van Suetendael et al.\nof analysts. Journal 2025.\n[2] Lee, K.: Other. 2019.\n")
    assert len(ents) == 2
    assert "598" not in ents[0].text and "of analysts" in ents[0].text
    assert ents[0].first_author == "Suetendael"


def test_doi_regex_priority_and_trailing_punct():
    ents = parse("1 Intro\nx\nReferences\n[1] Author, A.: Dataset. Zenodo (2021). https://doi.org/10.5281/zenodo.123456.\n"
                 "[2] Other, B.: T. 2020. doi:10.1007/978-3-031-\n82225-4_43).\n")
    assert ents[0].doi == "10.5281/zenodo.123456" and ents[0].resolution == "doi_in_text"
    assert ents[1].doi == "10.1007/978-3-031-82225-4_43"
    assert plan_resolution(ents) == []     # DOIs in the text: no Crossref request


def test_arxiv_id_forms():
    ents = parse("1 Intro\nx\nReferences\n[1] A, B.: T. arXiv:2406.01234v2 (2024)\n"
                 "[2] C, D.: T. https://arxiv.org/abs/1608.05678 (2016)\n")
    assert [e.arxiv for e in ents] == ["2406.01234v2", "1608.05678"]
    assert all(e.resolution == "arxiv_in_text" for e in ents)
    assert "arxiv:2406.01234" in ents[0].keys()


def test_first_author_year_and_keys():
    ents = parse("1 Intro\nx\nReferences\n[1] J. K. Smith, A. Lee: T. 2020.\n[2] Garc\u00eda, M.: T. 2018a.\n"
                 "[3] van der Aalst, W.M.P.: Process Mining. Springer (2016)\n[4] Doe JK, Roe L: T. 2015.\n")
    assert [(e.first_author, e.year) for e in ents] == [("Smith", "2020"), ("Garc\u00eda", "2018a"),
                                                         ("Aalst", "2016"), ("Doe", "2015")]
    assert {"n:2", "ay:garcia:2018"} <= ents[1].keys()


def test_unpublished_entries_never_planned():
    markers = ["in preparation", "Unpublished", "submitted", "under review", "forthcoming",
               "personal communication", "manuscript"]
    body = "".join(f"[{i + 1}] Author{i}, A.: Some work on things, {m}, 2024.\n" for i, m in enumerate(markers))
    ents = parse("1 Intro\nx\nReferences\n" + body)
    assert all(e.resolution == "not_sent_unpublished" for e in ents)
    assert plan_resolution(ents) == []


def _resolve(ref_string):
    ents = [RefEntry(n=1, text=ref_string, line_start=1, line_end=1)]
    reqs = plan_resolution(ents)
    assert len(reqs) == 1
    gate = Gate(build_plan("strict", "resolve", "d" * 64, reqs))
    opener = replay_opener(FIXTURES / "responses")
    planned = gate.requests()
    resp = send(planned[0], gate, tier="strict", round_name="resolve", opener=opener, sleep=lambda s: None)
    return apply_resolution(ents, {planned[0].subject: resp})[0], planned[0]


def test_accept_rule_on_recorded_crossref():
    e, req = _resolve(P["workA"]["ref_string"])
    assert e.resolution == "crossref_accepted" and e.resolved_doi == P["workA"]["doi"]
    assert e.title.startswith("Robotic Process Mining")
    assert req.carries == "reference_string" and req.subject == "ref:0"


def test_book_ref_rejected_low_score():
    e, _ = _resolve(P["book_ref"]["ref_string"])
    assert e.resolution == "crossref_low_score" and e.resolved_doi is None


def _crossref_body(*scores):
    items = [{"DOI": f"10.1/x{i}", "score": s, "title": [f"T{i}"]} for i, s in enumerate(scores)]
    return json.dumps({"message": {"items": items}}).encode()


def test_margin_rule_ambiguous():
    # synthetic scores
    for scores, expected in (((98.4, 97.4), "crossref_ambiguous"), ((98.4, 70.0), "crossref_accepted"),
                             ((98.4,), "crossref_accepted"), ((69.9, 10.0), "crossref_low_score")):
        ents = [RefEntry(n=1, text="x" * 30, line_start=1, line_end=1)]
        apply_resolution(ents, {"ref:0": Response("r001", 200, _crossref_body(*scores))})
        assert ents[0].resolution == expected, scores


def test_error_response_marks_backend_error():
    ents = [RefEntry(n=1, text="x" * 30, line_start=1, line_end=1), RefEntry(n=2, text="y" * 30, line_start=2, line_end=2)]
    apply_resolution(ents, {"ref:0": Response("r001", 500, b"", error="http_5xx"),
                            "ref:1": Response("r002", 200, b"not json")})
    assert [e.resolution for e in ents] == ["backend_error", "backend_error"]


def test_plan_carries_only_reference_strings(monkeypatch):
    monkeypatch.setenv("TEXT_PLAGIARISM_MAILTO", "me@example.org")
    body_text = "A secret sentence from the body that must never be sent anywhere at all."
    ents = parse(f"1 Intro\n{body_text}\nReferences\n[1] Smith, J.: A title for testing. Journal 1 (2020)\n")
    reqs = plan_resolution(ents)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.carries == "reference_string" and r.identity == "TEXT_PLAGIARISM_MAILTO"
    assert "secret" not in urllib.parse.unquote(r.url) and "Smith" in urllib.parse.unquote(r.url)
    assert "me@example.org" not in r.url
