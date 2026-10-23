"""Acceptance cases R1-R13 against a fake set of indexes (no network)."""
import io
import json
import subprocess
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import check_refs
import refs_lookup

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "reference-check" / "scripts" / "check_refs.py"


class FakeResp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def http_error(url, code, headers=None, body=b"{}"):
    return urllib.error.HTTPError(url, code, "x", headers or {}, io.BytesIO(body))


def cr(doi, title, authors, year, venue, short=None):
    return {"DOI": doi, "title": [title], "author": [{"family": a} for a in authors],
            "issued": {"date-parts": [[year]]}, "container-title": [venue],
            "short-container-title": [short] if short else [], "type": "journal-article", "URL": "https://doi.org/" + doi}


LENO = cr("10.1007/s12599-020-00641-4", "Robotic Process Mining: Vision and Challenges",
          ["Leno", "Polyvyanyy", "Dumas", "La Rosa", "Maggi"], 2020, "Business & Information Systems Engineering")
SOIL = cr("10.1000/soil", "Nitrogen Cycling in Alpine Soils", ["Garcia"], 2011, "Soil Science")
BERT = cr("10.18653/v1/n19-1423", "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
          ["Devlin", "Chang", "Lee", "Toutanova"], 2019, "Proceedings of the 2019 Conference of the North American Chapter of the Association for Computational Linguistics")


class Indexes:
    def __init__(self, crossref=None, datacite=None, handles=None, search=None, openalex=None, down=False,
                 status=None):
        self.crossref = {r["DOI"].lower(): r for r in (crossref or [])}
        self.datacite = {k.lower(): v for k, v in (datacite or {}).items()}
        self.handles = {h.lower() for h in (handles or [])}
        self.search = search or []
        self.openalex = openalex or []
        self.down, self.status = down, status
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if self.down:
            raise urllib.error.URLError("down")
        if self.status:
            raise http_error(url, self.status)
        p = urllib.parse.urlparse(url)
        if p.netloc == "api.crossref.org":
            if p.path.startswith("/works/"):
                doi = urllib.parse.unquote(p.path[len("/works/"):]).lower()
                if doi in self.crossref:
                    return FakeResp(json.dumps({"message": self.crossref[doi]}).encode())
                raise http_error(url, 404)
            return FakeResp(json.dumps({"message": {"items": self.search}}).encode())
        if p.netloc == "api.datacite.org":
            doi = urllib.parse.unquote(p.path[len("/dois/"):]).lower()
            if doi in self.datacite:
                return FakeResp(json.dumps({"data": {"attributes": self.datacite[doi]}}).encode())
            raise http_error(url, 404)
        if p.netloc == "api.openalex.org":
            return FakeResp(json.dumps({"results": self.openalex}).encode())
        if p.netloc == "doi.org":
            doi = urllib.parse.unquote(p.path[len("/api/handles/"):]).lower()
            if doi in self.handles:
                return FakeResp(json.dumps({"responseCode": 1}).encode())
            raise http_error(url, 404, body=json.dumps({"responseCode": 100}).encode())
        raise AssertionError("unexpected url " + url)


def bib(tmp_path, *entries):
    p = tmp_path / "refs.bib"
    p.write_text("\n".join(entries), encoding="utf-8")
    return p


def entry(key, title, author, year, doi=None, venue="", extra=""):
    d = f",\n  doi = {{{doi}}}" if doi else ""
    return (f"@article{{{key},\n  author = {{{author}}},\n  title = {{{title}}},\n  year = {{{year}}},\n"
            f"  journal = {{{venue}}}{d}{extra}\n}}")


def run(path, idx, **kw):
    return check_refs.build_report(path, opener=idx, **kw)


def verdicts(rep):
    return {r["id"]: r["verdict"] for r in rep["references"]}


def test_r1_verified_by_doi(tmp_path):
    p = bib(tmp_path, entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, Volodymyr and Polyvyanyy, Artem",
                            2020, "10.1007/s12599-020-00641-4", "Business & Information Systems Engineering"))
    rep = run(p, Indexes(crossref=[LENO]))
    assert verdicts(rep) == {"a": "verified"}
    assert rep["references"][0]["record"]["source"] == "crossref"
    assert rep["services_contacted"] == {"api.crossref.org": 1}


def test_r2_abbreviated_venue(tmp_path):
    p = bib(tmp_path, entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, V.", 2020,
                            "10.1007/s12599-020-00641-4", "Bus. Inf. Syst. Eng."))
    assert verdicts(run(p, Indexes(crossref=[LENO]))) == {"a": "verified"}


def test_r3_real_doi_wrong_work(tmp_path):
    p = bib(tmp_path, entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, Volodymyr", 2020, "10.1000/soil"))
    rep = run(p, Indexes(crossref=[SOIL]))
    assert verdicts(rep) == {"a": "mismatch"}
    assert {d["field"] for d in rep["references"][0]["differences"]} >= {"title", "authors", "year"}
    assert rep["needs_attention"][0]["id"] == "a"


def test_r4_doi_not_registered(tmp_path):
    p = bib(tmp_path, entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, V.", 2020, "10.9999/fake.123"))
    rep = run(p, Indexes(search=[LENO]))
    r = rep["references"][0]
    assert r["verdict"] == "doi_not_registered"
    assert r["alternative_record"]["doi"] == "10.1007/s12599-020-00641-4"  # same title under another DOI


def test_r5_plausible_but_unknown(tmp_path):
    p = bib(tmp_path, entry("a", "Neural Provenance Graphs for Exploratory Mining", "Smith, John", 2022))
    rep = run(p, Indexes(search=[LENO]))
    r = rep["references"][0]
    assert r["verdict"] == "not_found" and "indexes queried" in r["note"]
    assert "fabricat" not in json.dumps(rep["references"]).lower()
    assert "not that the work is fabricated" in rep["disclaimer"]


def test_r6_found_by_title_search(tmp_path):
    p = bib(tmp_path, entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, Volodymyr", 2020))
    rep = run(p, Indexes(search=[SOIL, LENO]))
    assert verdicts(rep) == {"a": "verified"} and rep["references"][0]["via"] == "title_search"


def test_r6b_openalex_fallback(tmp_path):
    p = bib(tmp_path, entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, Volodymyr", 2020))
    oa = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1007/s12599-020-00641-4",
          "title": "Robotic Process Mining: Vision and Challenges", "publication_year": 2020,
          "authorships": [{"author": {"display_name": "Volodymyr Leno"}}],
          "primary_location": {"source": {"display_name": "Business & Information Systems Engineering"}}}
    rep = run(p, Indexes(openalex=[oa]))
    assert verdicts(rep) == {"a": "verified"} and rep["references"][0]["record"]["source"] == "openalex"


def test_r7_arxiv_through_datacite(tmp_path):
    p = bib(tmp_path, "@misc{v, author={Devlin, Jacob and Chang, Ming-Wei}, title={BERT: Pre-training of Deep "
            "Bidirectional Transformers for Language Understanding}, year={2018}, eprint={1810.04805}, "
            "archivePrefix={arXiv}}")
    dc = {"10.48550/arxiv.1810.04805": {
        "doi": "10.48550/arXiv.1810.04805", "publicationYear": 2018,
        "titles": [{"title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"}],
        "creators": [{"familyName": "Devlin"}, {"familyName": "Chang"}], "publisher": "arXiv"}}
    idx = Indexes(datacite=dc)
    rep = run(p, idx)
    assert verdicts(rep) == {"v": "verified"}
    assert idx.urls[0].startswith("https://api.datacite.org/dois/10.48550/arXiv.1810.04805")


def test_r8_web_reference_unverifiable(tmp_path):
    p = bib(tmp_path, "@misc{w, title={Some site}, url={https://example.org/x}, howpublished={Web}}")
    idx = Indexes()
    rep = run(p, idx)
    assert verdicts(rep) == {"w": "unverifiable"} and idx.urls == []


def test_r9_latex_escapes_compared_after_normalisation(tmp_path):
    rec = cr("10.1000/mul", "A Study of Schrödinger Things", ["Müller", "García"], 2019, "Proc. of ICSE")
    p = bib(tmp_path, entry("m", r"A study of {S}chr{\"o}dinger things", r"M{\"u}ller, J{\"o}rg and Jos\'e Garc\'ia",
                            2019, "10.1000/mul", "Proc. of ICSE"))
    assert verdicts(run(p, Indexes(crossref=[rec]))) == {"m": "verified"}


def test_r10_duplicate_reference_looked_up_once(tmp_path):
    e = entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, V.", 2020, "10.1007/s12599-020-00641-4")
    p = bib(tmp_path, e, e.replace("{a,", "{b,"))
    idx = Indexes(crossref=[LENO])
    rep = run(p, idx)
    assert verdicts(rep) == {"a": "verified", "b": "verified"} and len(idx.urls) == 1
    assert rep["references"][1]["duplicate_of"] == "a"


def test_r11_service_down_is_not_clean(tmp_path):
    p = bib(tmp_path, entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, V.", 2020, "10.1007/s12599-020-00641-4"))
    rep = run(p, Indexes(down=True))
    assert verdicts(rep) == {"a": "lookup_failed"}
    assert any("could not be checked" in c for c in rep["coverage"]) and rep["service_errors"]
    assert rep["needs_attention"][0]["verdict"] == "lookup_failed"


def test_r11b_rate_limit_retries_then_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(refs_lookup, "sleep", lambda s: None)
    p = bib(tmp_path, entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, V.", 2020, "10.1007/s12599-020-00641-4"))
    idx = Indexes(status=429)
    rep = run(p, idx)
    assert verdicts(rep) == {"a": "lookup_failed"}
    assert len([u for u in idx.urls if "crossref" in u]) == refs_lookup.MAX_RETRIES + 1


def test_r12_no_reference_list(tmp_path):
    f = tmp_path / "paper.md"
    f.write_text("Only prose.\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(f)], capture_output=True, text=True)
    assert proc.returncode == 2 and json.loads(proc.stdout) == {"error": "no_references_found"}


def test_r13_offline_makes_no_requests(tmp_path):
    p = bib(tmp_path, entry("a", "Robotic Process Mining: Vision and Challenges", "Leno, V.", 2020, "10.1007/s12599-020-00641-4"))
    idx = Indexes(crossref=[LENO])
    rep = run(p, idx, offline=True)
    assert idx.urls == [] and verdicts(rep) == {"a": "unchecked"} and rep["requests_log"] == []
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(p), "--offline"], capture_output=True, text=True,
                          encoding="utf-8")
    assert proc.returncode == 0 and json.loads(proc.stdout)["references"][0]["verdict"] == "unchecked"


def test_text_paper_end_to_end(tmp_path):
    f = tmp_path / "paper.md"
    f.write_text("Intro.\n\n## References\n\n"
                 "Leno, V., Polyvyanyy, A. (2020). Robotic process mining: Vision and challenges. Business & "
                 "Information Systems Engineering. https://doi.org/10.1007/s12599-020-00641-4\n\n"
                 "Smith, J. (2021). A paper that does not exist at all. Journal of Imaginary Results, 4(2), 11-19.\n\n"
                 "Doe, A. (2019). Another invented title for this test. Journal of Tests, 1, 1-9.\n",
                 encoding="utf-8")
    rep = run(f, Indexes(crossref=[LENO]))
    assert [r["verdict"] for r in rep["references"]] == ["verified", "not_found", "not_found"]
    assert rep["summary"]["needs_attention"] == 2


def test_only_bibliographic_metadata_is_sent(tmp_path):
    p = bib(tmp_path, entry("a", "Neural Provenance Graphs", "Smith, John", 2022))
    idx = Indexes()
    run(p, idx)
    assert idx.urls and all(u.startswith(("https://api.crossref.org/", "https://api.openalex.org/")) for u in idx.urls)
    assert all("Neural" in urllib.parse.unquote(u) for u in idx.urls)
