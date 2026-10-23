import pytest

import refs_extract as rx

BIB = r"""
@article{leno2020,
  author = {Leno, Volodymyr and Polyvyanyy, Artem and Dumas, Marlon and La Rosa, Marcello and Maggi, Fabrizio Maria},
  title = {Robotic Process Mining: {Vision} and Challenges},
  journal = {Business \& Information Systems Engineering},
  year = {2020},
  doi = {10.1007/s12599-020-00641-4}
}
@inproceedings{muller,
  author = "M{\"u}ller, J{\"o}rg and Jos\'e Garc\'ia",
  title = "A study of {S}chr{\"o}dinger things",
  booktitle = {Proc. of ICSE},
  year = 2019
}
@misc{vaswani,
  author = {Vaswani, Ashish and others},
  title = {Attention Is All You Need},
  year = {2017},
  eprint = {1706.03762},
  archivePrefix = {arXiv}
}
@misc{site, title = {Some site}, url = {https://example.org/x}, howpublished = {Web}}
@comment{ignored}
"""


def test_bibtex_entries():
    refs = rx.parse_bibtex(BIB)
    assert [e["key"] for e in refs] == ["leno2020", "muller", "vaswani", "site"]
    r = [rx.bib_entry_to_ref(e) for e in refs]
    assert r[0]["doi"] == "10.1007/s12599-020-00641-4"
    assert r[0]["authors"][:2] == ["Leno", "Polyvyanyy"] and "La Rosa" in r[0]["authors"]
    assert r[0]["title"] == "Robotic Process Mining: Vision and Challenges"
    assert r[0]["venue"] == "Business & Information Systems Engineering"
    assert r[0]["year"] == 2020 and r[0]["kind"] == "identified"
    assert r[1]["authors"] == ["Müller", "García"]
    assert "Schrödinger" in r[1]["title"] and r[1]["year"] == 2019
    assert r[2]["arxiv"] == "1706.03762" and r[2]["et_al"] is True
    assert r[3]["kind"] == "web"


def test_unlatex():
    assert rx.unlatex(r"Erd\H{o}s and R\'enyi") == "Erdős and Rényi"
    assert rx.unlatex(r"{\"O}zt{\"u}rk") == "Öztürk"
    assert rx.unlatex(r"AT\&T \emph{Labs}") == "AT&T Labs"


APA = """Introduction text here.

References

Leno, V., Polyvyanyy, A., Dumas, M., La Rosa, M., & Maggi, F. M. (2020). Robotic process mining: Vision and challenges. Business & Information Systems Engineering, 63, 301-314. https://doi.org/10.1007/s12599-020-00641-4

Vaswani, A., Shazeer, N., Parmar, N. (2017). Attention is all you need. In Advances in Neural Information Processing Systems (pp. 5998-6008).

Smith, J. (2021). A paper that does not exist. Journal of Imaginary Results, 4(2), 11-19.
"""


def test_split_blank_line_separated():
    raws = rx.split_reference_section(APA)
    assert len(raws) == 3


def test_parse_apa_entry():
    raws = rx.split_reference_section(APA)
    a = rx.parse_reference_text(raws[0], "r1")
    assert a["doi"] == "10.1007/s12599-020-00641-4"
    assert a["year"] == 2020
    assert a["title"].lower().startswith("robotic process mining")
    assert a["authors"][0] == "Leno" and "Maggi" in a["authors"]
    c = rx.parse_reference_text(raws[2], "r3")
    assert c["doi"] is None and c["title"] == "A paper that does not exist" and c["authors"] == ["Smith"]
    assert c["venue"] and c["venue"].startswith("Journal of Imaginary")


NUMBERED = """Body.

## References

[1] A. Author, B. Writer, "A fine title for testing," IEEE Trans. Testing, vol. 3, pp. 1-10, 2018.
[2] C. Third and D. Fourth, "Another study of things," in Proc. Conf., 2019, doi:10.1109/ABC.2019.1234.
[3] E. Fifth, "Third title here for tests," Journal of Tests, 2020.

## Appendix A
Not a reference [4] foo
"""


def test_numbered_ieee_split_and_parse():
    raws = rx.split_reference_section(NUMBERED)
    assert len(raws) == 3
    r1 = rx.parse_reference_text(raws[0], "r1")
    assert r1["title"] == "A fine title for testing" and r1["year"] == 2018 and r1["authors"][0] == "Author"
    r2 = rx.parse_reference_text(raws[1], "r2")
    assert r2["doi"] == "10.1109/ABC.2019.1234"


def test_doi_trailing_punctuation_and_arxiv():
    r = rx.parse_reference_text("X. Y. Title of it, arXiv:2101.00001v2, 2021. doi:10.1000/abc.def).", "r")
    assert r["doi"] == "10.1000/abc.def" and r["arxiv"] == "2101.00001"


def test_web_reference_kind():
    r = rx.parse_reference_text("Some Org. Guide to things. Retrieved May 2, 2023, from https://example.org/guide", "r")
    assert r["kind"] == "web" and r["url"] == "https://example.org/guide"


def test_bibitems():
    tex = r"""\begin{thebibliography}{9}
\bibitem{a} A. Author, \newblock ``A title,'' \emph{Journal}, 2018.
\bibitem{b} B. Other. A Second Title. Venue, 2019. doi:10.1000/xyz
\end{thebibliography}"""
    refs = rx.parse_bibitems(tex)
    assert [r["id"] for r in refs] == ["a", "b"]
    assert refs[1]["doi"] == "10.1000/xyz"


def test_extract_files(tmp_path):
    (tmp_path / "r.bib").write_text(BIB, encoding="utf-8")
    out = rx.extract_references(tmp_path / "r.bib")
    assert out["source"] == "bibtex" and len(out["references"]) == 4
    (tmp_path / "p.md").write_text(APA, encoding="utf-8")
    assert len(rx.extract_references(tmp_path / "p.md")["references"]) == 3


def test_tex_with_bibliography_file(tmp_path):
    (tmp_path / "refs.bib").write_text(BIB, encoding="utf-8")
    (tmp_path / "main.tex").write_text(r"\bibliography{refs}", encoding="utf-8")
    assert len(rx.extract_references(tmp_path / "main.tex")["references"]) == 4


@pytest.mark.parametrize("name,content,code", [
    ("none.md", "Just prose, no list at all.", "no_references_found"),
    ("empty.txt", "   \n", "no_text_extracted"),
    ("x.xyz", "References", "unsupported_format"),
    ("nothing.bib", "no entries here", "no_references_found"),
])
def test_errors(tmp_path, name, content, code):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    with pytest.raises(rx.ExtractError) as e:
        rx.extract_references(p)
    assert e.value.code == code


def test_missing_file(tmp_path):
    with pytest.raises(rx.ExtractError) as e:
        rx.extract_references(tmp_path / "nope.md")
    assert e.value.code == "file_not_found"
