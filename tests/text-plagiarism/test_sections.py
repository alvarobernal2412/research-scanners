from extract import extract_bytes
from sections import EXCLUDED_KINDS, default_open_sections, detect_sections, excluded_lines


def _doc(text, fmt="txt"):
    return extract_bytes(text.encode("utf-8"), fmt)


def test_md_headings_make_sections_front_before_first():
    doc = _doc("Title line\n\n# Introduction\nSome intro words here.\n# Methods\nMore words.\n", "md")
    secs = detect_sections(doc)
    assert [s.id for s in secs] == ["s1", "s2", "s3"]
    assert [s.kind for s in secs] == ["front", "body", "body"]
    assert secs[1].heading == "Introduction" and secs[1].start_line == 3 and secs[1].end_line == 4
    assert secs[1].words == 4


def test_numbered_pdf_headings_detected():
    text = ("1 Introduction\nWe study things.\n2.1 Related Work\nOthers did stuff.\n"
            "2.1 is the value.\nThis is a long line that starts with a number 3 Words and keeps going on and on and on forever\n")
    secs = detect_sections(_doc(text))
    assert [s.heading for s in secs] == ["1 Introduction", "2.1 Related Work"]
    long_line = "3 Words and keeps going on and on and on forever and ever more words here"
    secs = detect_sections(_doc("1 Intro\nx y z.\n" + long_line + "\n"))
    assert len(secs) == 1


def test_known_unnumbered_names():
    names = {"References": "references", "Bibliography": "references", "Acknowledgements": "acknowledgements",
             "Appendix A": "appendix", "Funding": "statements", "Data Availability": "statements",
             "Conflict of Interest": "statements"}
    for name, kind in names.items():
        secs = detect_sections(_doc(f"1 Intro\nbody words.\n{name}\nsome text here.\n"))
        assert secs[-1].kind == kind, name


def test_references_run_to_next_heading_or_end():
    text = "1 Intro\nbody.\nReferences\n[1] A. B. Title. 2020.\n[2] C. D. Other. 2019.\nAppendix A\nextra material\n"
    secs = detect_sections(_doc(text))
    assert [s.kind for s in secs] == ["body", "references", "appendix"]
    assert secs[1].end_line == 5


def test_numbered_reference_entries_do_not_open_sections():
    text = "1 Intro\nbody.\nReferences\n1. Author A\n2. Author B\n"
    secs = detect_sections(_doc(text))
    assert [s.kind for s in secs] == ["body", "references"]


def test_md_subsection_of_references_stays_references():
    text = "# Intro\nbody.\n## References\n[1] x\n### Primary sources\n[2] y\n# Appendix\nz\n"
    secs = detect_sections(_doc(text, "md"))
    assert [s.kind for s in secs] == ["body", "references", "references", "appendix"]


def test_excluded_lines_include_md_footnote_definitions():
    text = "# Intro\nA sentence[^1] here.\n\n[^1]: The footnote text.\n    continued here.\n\nafter.\n# References\n[1] r\n"
    doc = _doc(text, "md")
    secs = detect_sections(doc)
    ex = excluded_lines(doc, secs)
    assert {4, 5} <= ex and {8, 9} <= ex
    assert 2 not in ex and 7 not in ex
    assert "references" in EXCLUDED_KINDS and "appendix" not in EXCLUDED_KINDS


def test_default_open_sections_picks_intro_and_related_work():
    text = "# Introduction\na b.\n# Related Work\nc d.\n# Method\ne f.\n# Acknowledgements\ng\n"
    secs = detect_sections(_doc(text, "md"))
    assert default_open_sections(secs) == ["s1", "s2"]


def test_no_headings_one_body_section():
    secs = detect_sections(_doc("just some text\nand more\n"))
    assert len(secs) == 1 and secs[0].kind == "body" and secs[0].words == 5


def test_tex_bibliography_environment_is_references():
    tex = "\\section{Intro}\nText.\n\\begin{thebibliography}{9}\n\\bibitem{a} X.\n\\end{thebibliography}\n"
    secs = detect_sections(_doc(tex, "tex"))
    assert [s.kind for s in secs] == ["body", "references"]
