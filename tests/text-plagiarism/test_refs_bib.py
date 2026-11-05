"""LaTeX documents whose bibliography lives in external .bib (or a compiled .bbl) files."""
from extract import extract
from prepare import prepare
from refs import bibliography_from_tex, parse_bib_entries

BIB = r"""
@string{acm = "ACM"}
@article{smith20, author = {Smith, John and M{\"u}ller, Anna}, title = {A {Study} \& Stuff},
  year = {2020}, doi = {10.1234/ABC.5}}
@inproceedings{lee19, author = "Lee, Kim", title = "No identifier", year = 2019}
@book{unused99, author = {Nobody, N.}, title = {Never cited}, year = {1999}}
"""
TEX = (r"\section{Intro}" "\n" r"See \cite{smith20} and \citep[p.~3]{lee19, missing00}." "\n"
       r"% \cite{unused99}" "\n" r"\bibliography{refs}" "\n")
BBL = (r"\begin{thebibliography}{9}" "\n" r"\bibitem{smith20} Smith, J.: T. 2020." "\n"
       r"\end{thebibliography}" "\n")


def write(tmp_path, bib=BIB):
    (tmp_path / "main.tex").write_text(TEX, encoding="utf-8")
    if bib is not None:
        (tmp_path / "refs.bib").write_text(bib, encoding="utf-8")
    return tmp_path / "main.tex"


def test_bib_entries_and_cited_only_references(tmp_path):
    ents = parse_bib_entries(BIB)
    assert [e["key"] for e in ents] == ["smith20", "lee19", "unused99"] and ents[1].get("doi") is None
    assert ents[0]["title"] == "A Study & Stuff" and ents[0]["authors"] == ["Smith, John", "Müller, Anna"]
    path = write(tmp_path)
    refs, notes = bibliography_from_tex(extract(path), path)
    assert [r.tex_key for r in refs] == ["smith20", "lee19"]
    assert refs[0].doi == "10.1234/abc.5" and refs[0].first_author == "Smith" and refs[0].year == "2020"
    assert refs[1].resolution == "not_attempted" and "No identifier" in refs[1].text
    assert notes == ["cited_key_not_in_bib:missing00"]


def test_missing_bib_falls_back_to_bbl(tmp_path):
    path = write(tmp_path, bib=None)
    assert bibliography_from_tex(extract(path), path) == ([], ["bib_file_not_found:refs.bib"])
    (tmp_path / "main.bbl").write_text(BBL, encoding="utf-8")
    refs, _ = bibliography_from_tex(extract(path), path)
    assert [r.tex_key for r in refs] == ["smith20"]


def test_prepare_uses_bib(tmp_path):
    summary, _ = prepare(write(tmp_path), tmp_path / "w")
    assert summary["references"]["count"] == 2 and "references_not_found" not in summary["warnings"]


def test_arxiv_id_and_doi_read_from_note_fields():
    ents = parse_bib_entries("@misc{a, title={T}, year={2026}, note={arXiv:2604.22750}}\n"
                             "@misc{b, title={U}, year={2025}, howpublished={https://arxiv.org/abs/2512.18470}}\n"
                             "@misc{c, title={V}, year={2020}, note={doi:10.1234/xyz}}\n")
    assert [e.get("arxiv") for e in ents] == ["2604.22750", "2512.18470", None] and ents[2]["doi"] == "10.1234/xyz"
    import corpus
    recs = corpus.parse_bib("@misc{a, title={T}, year={2026}, note={arXiv:2604.22750}}")
    assert recs[0]["arxiv"] == "2604.22750" and recs[0]["identifier"] == "arXiv:2604.22750"
