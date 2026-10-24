import base64
import json

import acquire
import net
import pytest
from acquire import (SourceInfo, apply_download, apply_locate, apply_zenodo, load_source_lines, plan_download,
                     plan_locate, plan_zenodo, settle, text_from_response)
from approval import STRICT_CARRIES, Gate, build_plan
from conftest import FIXTURES, make_pdf
from net import Response, replay_opener, send

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))
RESP = FIXTURES / "responses"


@pytest.fixture(autouse=True)
def _reset():
    net.reset_rate_limits()


def body_of(name):
    return base64.b64decode(json.loads((RESP / f"{name}.json").read_text(encoding="utf-8"))["body_b64"])


def fetch(reqs, opener=None):
    plan = build_plan("strict", "t", "d" * 64, reqs)
    gate = Gate(plan)
    opener = opener or replay_opener(RESP)
    out = []
    for r in gate.requests():
        out.append((r, send(r, gate, tier="strict", round_name="t", opener=opener, sleep=lambda s: None)))
    return out


def located(doi, title=None, key="1"):
    info = SourceInfo(key=key, ref_n=1, doi=doi, title=title)
    [(r, resp)] = fetch(plan_locate([info]))
    assert resp.status == 200, doi
    return apply_locate(info, resp.body)


def test_locate_workA_yields_pdf_candidate_with_license():
    info = located(P["workA"]["doi"], P["workA"]["title"])
    pdf = "https://link.springer.com/content/pdf/10.1007/s12599-020-00641-4.pdf"
    assert info.candidates[0] == pdf and info.is_oa and info.oa_status == "hybrid"
    assert info.cand_meta[pdf] == {"license": "cc-by", "version": "publishedVersion"}
    assert info.status == "pending"


def test_t1_paywalled_publisher_location_skipped_zenodo_planned():
    info = located(P["t1"]["doi"], "A Conceptual Model to Enhance Process Mining Analysis with Provenance")
    assert info.candidates == [] and info.status == "pending"
    assert info.zenodo_ids == ["21373046", "21373047"]
    reqs = plan_zenodo([info])
    assert [r.url for r in reqs] == ["https://zenodo.org/api/records/21373046", "https://zenodo.org/api/records/21373047"]


def test_zenodo_appendix_only_is_not_full_text():
    info = located(P["t1"]["doi"], "A Conceptual Model to Enhance Process Mining Analysis with Provenance")
    for r, resp in fetch(plan_zenodo([info])):
        zid = r.url.rsplit("/", 1)[-1]
        apply_zenodo(info, resp.body, zid)
    assert info.candidates == [] and info.reason == "only_supplementary_files"
    settle([info])
    assert info.status == "not_obtained" and info.reason == "only_supplementary_files"


def test_zenodo_publication_pdf_becomes_candidate_and_unrelated_record_does_not():
    rec = {"metadata": {"title": "Process mining with provenance", "resource_type": {"type": "publication"},
                        "license": {"id": "cc-by-4.0"}},
           "files": [{"key": "paper.pdf", "links": {"self": "https://zenodo.org/api/records/9/files/paper.pdf/content"}},
                     {"key": "appendix.pdf", "links": {"self": "https://zenodo.org/x"}}]}
    info = SourceInfo("1", 1, "10.5281/zenodo.9", "Process mining with provenance")
    apply_zenodo(info, json.dumps(rec).encode(), "9")
    assert info.candidates == ["https://zenodo.org/api/records/9/files/paper.pdf/content"]
    assert info.cand_meta[info.candidates[0]]["license"] == "cc-by-4.0"
    other = SourceInfo("2", 2, "10.5281/zenodo.9", "A wholly different title about birds")
    apply_zenodo(other, json.dumps(rec).encode(), "9")
    assert other.candidates == [] and other.reason == "only_supplementary_files"


def test_not_oa_without_locations_is_paywalled():
    info = SourceInfo("1", 1, "10.1/x", "T")
    apply_locate(info, json.dumps({"open_access": {"is_oa": False}, "locations": []}).encode())
    assert info.status == "not_obtained" and info.reason == "paywalled"
    info = SourceInfo("2", 2, "10.1/y", "T")
    apply_locate(info, json.dumps({"open_access": {"is_oa": True}, "locations": []}).encode())
    assert info.reason == "no_oa_location"
    info = SourceInfo("3", 3, "10.1/z", "T")
    apply_locate(info, b"not json")
    assert info.reason == "backend_error"


def test_pmcid_prefers_europepmc_xml():
    work = json.loads(body_of("oa-work-D"))
    work["ids"]["pmcid"] = "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8529480"
    info = SourceInfo("1", 1, P["pmc"]["doi"], None)
    apply_locate(info, json.dumps(work).encode())
    assert info.candidates[0] == "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC8529480/fullTextXML"
    assert info.candidates[1] == "https://www.jmir.org/2021/10/e27499/PDF" and info.pmcid == "PMC8529480"
    assert acquire.service_for_url(info.candidates[0]) == "europepmc"


def test_arxiv_and_version_ordering():
    work = {"open_access": {"is_oa": True}, "best_oa_location": None, "locations": [
        {"is_oa": True, "pdf_url": "https://repo.example.org/sub.pdf", "version": "submittedVersion"},
        {"is_oa": True, "pdf_url": "https://repo.example.org/acc.pdf", "version": "acceptedVersion"},
        {"is_oa": True, "pdf_url": None, "landing_page_url": "https://arxiv.org/abs/2406.01234v2", "version": "submittedVersion"}]}
    info = SourceInfo("1", 1, "10.1/x", None)
    apply_locate(info, json.dumps(work).encode())
    assert info.candidates == ["https://repo.example.org/acc.pdf", "https://repo.example.org/sub.pdf",
                               "https://arxiv.org/pdf/2406.01234"]


def test_download_detects_pdf_by_magic_not_content_type():
    pdf = make_pdf(["Octet stream served documents are still pdf files here."])
    lines, reason = text_from_response(Response("r", 200, pdf, {"content-type": "application/octet-stream"}))
    assert reason is None and lines[0].text.startswith("Octet stream")


def test_jats_xml_body_extracted_without_ref_list():
    lines, reason = text_from_response(Response("r", 200, body_of("epmc-fulltext-D"), {}))
    assert reason is None
    text = " ".join(l.text for l in lines)
    assert P["pmc"]["sentences"][0] in text.replace("’", "'")
    assert "Cite as" not in text and "doi.org" not in text


def test_html_landing_page_not_full_text():
    lines, reason = text_from_response(Response("r", 200, body_of("pdf-A-challenge"), {"content-type": "text/html"}))
    assert lines is None and reason == "landing_page_only"
    assert text_from_response(Response("r", 200, b"<!DOCTYPE html><html><body>x</body></html>"))[1] == "landing_page_only"
    assert text_from_response(Response("r", 403, b"", error="http_4xx"))[1] == "download_failed"
    assert text_from_response(Response("r", 200, b"\x00\x01binary"))[1] == "download_failed"


def test_glued_source_text_low_text_quality():
    glued = "Forcompleteness,Table1reproducesthefollowingresultsinfulldetailaboutthemethodsused"
    pdf = make_pdf([glued, glued, glued, "some normal words here too"])
    lines, reason = text_from_response(Response("r", 200, pdf, {}))
    assert lines is None and reason == "low_text_quality"
    assert text_from_response(Response("r", 200, make_pdf([]), {}))[1] == "no_text"


def test_failed_download_falls_back_to_next_candidate(tmp_path):
    info = SourceInfo("1", 1, "10.1/x", "T", candidates=["https://a.example.org/1.pdf", "https://b.example.org/2.pdf"])
    pdf = make_pdf(["A second candidate that works fine for the fallback test."])
    [r1] = plan_download([info])
    assert r1.url == "https://a.example.org/1.pdf" and r1.carries == "public_url"
    apply_download(info, Response("r001", 200, body_of("pdf-A-challenge")), tmp_path, url=r1.url)
    assert info.status == "pending" and info.reason == "landing_page_only"
    [r2] = plan_download([info])
    assert r2.url == "https://b.example.org/2.pdf"
    apply_download(info, Response("r001", 200, pdf), tmp_path, url=r2.url)
    assert info.status == "obtained" and info.url == r2.url and info.reason is None
    assert plan_download([info]) == []
    assert load_source_lines(info)[0].text.startswith("A second candidate")


def test_failed_downloads_end_as_not_obtained(tmp_path):
    info = SourceInfo("1", 1, "10.1/x", "T", candidates=["https://a.example.org/1.pdf"])
    [r] = plan_download([info])
    apply_download(info, Response("r001", 500, b"", error="http_5xx"), tmp_path, url=r.url)
    assert info.status == "not_obtained" and info.reason == "download_failed"


def test_store_dir_under_cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_PLAGIARISM_CACHE", str(tmp_path / "cache"))
    info = SourceInfo("1", 1, "10.1/x", "T", candidates=["https://a.example.org/1.pdf"],
                      cand_meta={"https://a.example.org/1.pdf": {"license": "cc-by", "version": "publishedVersion"}})
    store = net.cache_root() / "sources"
    apply_download(info, Response("r001", 200, make_pdf(["Some text for storage in the cache."])), store,
                   url="https://a.example.org/1.pdf")
    from pathlib import Path
    assert (tmp_path / "cache") in Path(info.text_path).parents
    saved = json.loads(Path(info.text_path).read_text(encoding="utf-8"))
    assert saved["license"] == "cc-by" and saved["version"] == "publishedVersion" and saved["doi"] == "10.1/x"


def test_locate_requests_carry_only_public_ids(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "abc12345")
    infos = [SourceInfo("1", 1, "10.1007/978-3-032-40795-5_31", "Title text must not be sent"),
             SourceInfo("2", 2, "10.5281/zenodo.21373047", None)]
    reqs = plan_locate(infos)
    assert len(reqs) == 2
    for r in reqs:
        assert r.carries in STRICT_CARRIES and "Title text" not in r.url and r.text_sent is None
        assert r.url.endswith("api_key={OPENALEX_API_KEY}") and r.identity == "OPENALEX_API_KEY"
        assert "abc12345" not in r.url
    assert infos[1].zenodo_ids == ["21373047"]
    assert reqs[0].url.startswith("https://api.openalex.org/works/doi:10.1007/978-3-032-40795-5_31")
