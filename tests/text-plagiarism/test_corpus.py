import base64
import json
import random
import time

import corpus
import prepare
import strict
from conftest import FIXTURES, blank_pdf, make_pdf
from extract import Line
from findings import combined_findings, corpus_findings, strict_findings
from helpers import call_main, drive_strict, write_md
from net import replay_opener

P = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))
A = P["workA"]
REPLAY = replay_opener(FIXTURES / "responses")


def pdf_a(path):
    rec = json.loads((FIXTURES / "responses" / "pdf-A.json").read_text(encoding="utf-8"))
    path.write_bytes(base64.b64decode(rec["body_b64"]))
    return path


def tripwire(*a, **k):
    raise AssertionError("a request was made")


def prep(tmp_path, body, corpus_specs, refs=None, name="doc.md"):
    refs_text = "# References\n\n" + "\n".join(refs) + "\n" if refs else ""
    doc = write_md(tmp_path, name, "# Introduction\n\n" + body + "\n\n" + refs_text)
    wd = tmp_path / "wd"
    args = [str(doc), "--workdir", str(wd)]
    for s in corpus_specs:
        args += ["--corpus", str(s)]
    code, out = call_main(prepare.main, args)
    assert code == 0, out
    return wd, out


def test_directory_corpus_loads_supported_files_and_reports_unsupported(tmp_path):
    d = tmp_path / "papers"
    d.mkdir()
    pdf_a(d / "a.pdf")
    (d / "notes.txt").write_text("Some plain notes about process mining and event logs, long enough to be text.", "utf-8")
    (d / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "broken.pdf").write_bytes(b"this is not a pdf")
    items = corpus.load_corpus([d], "0" * 64)
    by = {i.name: i for i in items}
    assert by["a.pdf"].status == "loaded" and by["a.pdf"].words > 5000
    assert by["notes.txt"].status == "loaded"
    assert by["figure.png"].status == "unsupported" and "png" in by["figure.png"].reason
    assert by["broken.pdf"].status == "unreadable"


def test_bib_and_txt_identifier_lists_become_pending_fetch(tmp_path):
    bib = tmp_path / "refs.bib"
    bib.write_text('@article{a, title={Robotic {Process} Mining}, author={Leno, V.}, year={2020}, doi={10.1007/S12599-020-00641-4}}\n'
                   '@misc{b, title={Preprint}, eprint={2406.01234}}\n@book{c, title={No identifier}, year={1977}}\n'
                   '@online{d, title={Web}, url={https://example.org/paper.pdf}}\n', "utf-8")
    lst = tmp_path / "ids.txt"
    lst.write_text("# my list\n10.2196/27499\nhttps://doi.org/10.1186/s12911-021-01725-1\narXiv:1608.05678v2\n"
                   "https://example.org/x.pdf\n", "utf-8")
    items = corpus.load_corpus([bib, lst], "0" * 64)
    assert all(i.status == "pending_fetch" and i.kind == "identifier" for i in items)
    assert [i.identifier for i in items] == ["10.1007/s12599-020-00641-4", "arXiv:2406.01234",
                                             "https://example.org/paper.pdf", "10.2196/27499",
                                             "10.1186/s12911-021-01725-1", "arXiv:1608.05678",
                                             "https://example.org/x.pdf"]
    assert items[0].title == "Robotic Process Mining" and items[0].doi == "10.1007/s12599-020-00641-4"
    assert items[4].doi == "10.1186/s12911-021-01725-1"


def test_manifest_metadata_applies_and_doi_from_text_is_fallback_only(tmp_path):
    d = tmp_path / "papers"
    d.mkdir()
    pdf_a(d / "a.pdf")
    plain = load = corpus.load_corpus([d], "0" * 64)[0]
    assert plain.doi == A["doi"] and "doi_from_text" in plain.warnings and plain.title_source == "text_guess"
    (d / "corpus.json").write_text(json.dumps([{"file": "a.pdf", "doi": "10.9999/manifest", "title": "My Title Here",
                                                "authors": "Leno, V.", "year": 2020, "relation": "own"}]), "utf-8")
    [item] = corpus.load_corpus([d], "0" * 64)
    assert item.doi == "10.9999/manifest" and "doi_from_text" not in item.warnings
    assert item.title == "My Title Here" and item.title_source == "manifest" and item.relation == "own"
    assert item.year == 2020 and item.authors == "Leno, V."
    [explicit] = corpus.load_corpus([d / "corpus.json"], "0" * 64)
    assert explicit.doi == "10.9999/manifest"


def test_scanned_pdf_in_corpus_listed_no_text_extracted_not_counted(tmp_path):
    p = tmp_path / "scan.pdf"
    p.write_bytes(blank_pdf())
    [item] = corpus.load_corpus([p], "0" * 64)
    assert item.status == "no_text_extracted" and item.words == 0


def test_same_file_as_document_is_skipped(tmp_path):
    doc = write_md(tmp_path, "doc.md", "# Intro\nThe document under check, with some words in it.\n")
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(doc), "--workdir", str(wd), "--corpus", str(doc)])
    [row] = corpus.item_table(wd)
    assert row["status"] == "same_as_document"


def test_caps_report_over_cap_items(tmp_path, monkeypatch):
    d = tmp_path / "many"
    d.mkdir()
    for i in range(4):
        (d / f"f{i}.txt").write_text(f"File number {i} with enough words to be a text document for the test.", "utf-8")
    monkeypatch.setattr(corpus, "MAX_CORPUS_FILES", 2)
    items = corpus.load_corpus([d], "0" * 64)
    assert [i.status for i in items] == ["loaded", "loaded", "over_cap", "over_cap"]
    monkeypatch.setattr(corpus, "MAX_CORPUS_FILES", 200)
    monkeypatch.setattr(corpus, "MAX_CORPUS_FILE_BYTES", 10)
    assert {i.status for i in corpus.load_corpus([d], "0" * 64)} == {"too_large"}
    monkeypatch.setattr(corpus, "MAX_CORPUS_FILE_BYTES", 50_000_000)
    monkeypatch.setattr(corpus, "MAX_CORPUS_TOTAL_BYTES", 150)
    assert [i.status for i in corpus.load_corpus([d], "0" * 64)].count("over_cap") >= 1


def test_running_headers_not_a_match(tmp_path):
    header = "Proceedings of the International Conference on Process Mining Research and Applications Workshop Track"
    pages = []
    for p in range(1, 5):
        pages += [f"{header} {p}", f"Unique content line {p} alpha beta gamma delta epsilon zeta eta."]
    src = tmp_path / "paper.pdf"
    src.write_bytes(make_pdf(pages, pages=4))
    [item] = corpus.load_corpus([src], "0" * 64, tmp_path / "wd")
    assert item.status == "loaded"
    wd, _ = prep(tmp_path, header + " 2026 is where this was presented in our own words.", [src])
    assert corpus_findings(wd)["findings"] == []


def test_unattributed_copy_from_local_corpus_flagged(tmp_path):
    src = pdf_a(tmp_path / "work-a.pdf")
    body = f"{A['control_sentence']} {A['miss_sentence']}"
    wd, out = prep(tmp_path, body, [src])
    assert out["corpus"]["counts"] == {"loaded": 1} and out["corpus"]["local_only"] == ["work-a.pdf"]
    res = corpus_findings(wd)
    assert len(res["findings"]) == 2
    for f in res["findings"]:
        assert f["attribution"] == "unattributed" and f["severity"] == "flag"
        assert f["source"]["found_via"] == "user_corpus"
        assert f["source"]["doi"] == A["doi"] or f["source"]["url"] == "file:work-a.pdf"
    assert not (wd / "requests.jsonl").exists()


def test_cited_not_quoted_and_quoted_cited_with_corpus_item_in_reference_list(tmp_path):
    d = tmp_path / "papers"
    d.mkdir()
    pdf_a(d / "a.pdf")
    (d / "corpus.json").write_text(json.dumps([{"file": "a.pdf", "title": A["title"]}]), "utf-8")
    refs = [f"[1] {A['ref_string']}"]
    wd, _ = prep(tmp_path, A["passage"] + " [1]", [d], refs)
    [f] = corpus_findings(wd)["findings"]
    assert f["attribution"] == "cited_not_quoted" and f["severity"] == "light" and f["reference_entry"] == 1
    wd2 = tmp_path / "wd2"
    doc2 = write_md(tmp_path, "doc2.md", '# Introduction\n\nAs said, "' + A["passage"] + '" [1]\n\n# References\n\n'
                    + refs[0] + "\n")
    call_main(prepare.main, [str(doc2), "--workdir", str(wd2), "--corpus", str(d)])
    res = corpus_findings(wd2)
    assert res["findings"] == [] and res["informational"][0]["attribution"] == "quoted_cited"


def test_unrelated_corpus_gives_no_findings(tmp_path):
    rnd = random.Random(7)
    words = [f"zzq{rnd.randrange(10**5)}w" for _ in range(300)]
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text(" ".join(words), "utf-8")
    wd, _ = prep(tmp_path, A["passage"], [unrelated])
    res = corpus_findings(wd)
    assert res == {"findings": [], "informational": [], "boilerplate": [],
                   "sources_compared": res["sources_compared"]} and len(res["sources_compared"]) == 1


def test_own_relation_is_self_overlap_light(tmp_path):
    d = tmp_path / "mine"
    d.mkdir()
    pdf_a(d / "earlier.pdf")
    (d / "corpus.json").write_text(json.dumps([{"file": "earlier.pdf", "title": "My earlier paper on things",
                                                "relation": "own"}]), "utf-8")
    wd, _ = prep(tmp_path, A["passage"], [d])
    [f] = corpus_findings(wd)["findings"]
    assert f["kind"] == "self_overlap" and f["severity"] == "light" and f["attribution"] == "unattributed"


def test_same_work_in_references_and_corpus_compared_once(tmp_path):
    src = pdf_a(tmp_path / "a.pdf")
    refs = [f"[1] {A['ref_string']} https://doi.org/{A['doi']}"]
    wd, _ = prep(tmp_path, A["passage"] + " [1]", [src], refs)
    drive_strict(wd, REPLAY)
    res = combined_findings(wd)
    assert len(res["findings"]) == 1 and res["boilerplate"] == []
    assert len(res["sources_compared"]) == 1
    f = res["findings"][0]
    assert f["attribution"] == "cited_not_quoted" and f["source"]["license"] == "cc-by"   # local text, reference metadata
    assert strict_findings(wd)["sources_compared"][0]["doi"] == A["doi"]


def test_identifier_items_join_strict_rounds_and_missing_text_is_reported(tmp_path):
    lst = tmp_path / "ids.txt"
    lst.write_text(f"{A['doi']}\n{P['t1']['doi']}\n", "utf-8")
    wd, out = prep(tmp_path, A["passage"], [lst])
    assert out["corpus"]["identifiers"] == 2
    drive_strict(wd, REPLAY)
    rows = {r["identifier"]: r for r in corpus.item_table(wd)}
    assert rows[A["doi"]]["status"] == "loaded"
    assert rows[P["t1"]["doi"]]["status"] == "full_text_not_obtained"
    assert rows[P["t1"]["doi"]]["reason"] == "paywalled_provide_pdf"
    res = corpus_findings(wd)
    assert len(res["findings"]) == 1 and res["findings"][0]["source"]["found_via"] == "user_corpus"
    assert strict_findings(wd)["findings"] == []     # strict_findings covers references only


def test_local_corpus_files_produce_no_requests(tmp_path):
    src = pdf_a(tmp_path / "a.pdf")
    wd, _ = prep(tmp_path, A["passage"], [src])
    code, plan = call_main(strict.main, ["plan", "--workdir", str(wd)])
    assert code == 0 and plan == {"done": True}
    drive_strict(wd, tripwire)
    assert not (wd / "requests.jsonl").exists()
    code, out = call_main(corpus.main, ["status", "--workdir", str(wd)])
    assert code == 0 and out["items"][0]["status"] == "loaded" and "path" not in out["items"][0]


def test_local_paths_and_text_never_in_any_plan_request(tmp_path):
    secret_dir = tmp_path / "confidential-drafts"
    secret_dir.mkdir()
    sentence = P["t2_standin"]["sentence"]
    (secret_dir / "private-draft.txt").write_text(sentence + " Further private words follow in this unpublished file.", "utf-8")
    lst = tmp_path / "ids.txt"
    lst.write_text(A["doi"] + "\n", "utf-8")
    wd, _ = prep(tmp_path, "Our body words are different.", [secret_dir, lst])
    code, plan = call_main(strict.main, ["plan", "--workdir", str(wd)])
    assert plan["preview"]["local_only"] == {"count": 1, "files": ["private-draft.txt"]}
    text = open(plan["plan"], encoding="utf-8").read()
    for needle in ("confidential-drafts", "private-draft", "unpublished file", "preliminary stage", "tidy tables"):
        assert needle not in text
    drive_strict(wd, REPLAY)
    log = (wd / "requests.jsonl").read_text(encoding="utf-8")
    assert "private-draft" not in log and "tidy tables" not in log


def test_unpublished_draft_stand_in_is_found(tmp_path):
    draft = tmp_path / "other-author-draft.txt"
    draft.write_text("Draft chapter. " + P["t2_standin"]["sentence"] + " The rest of the draft continues elsewhere.", "utf-8")
    wd, _ = prep(tmp_path, "Intro words. " + P["t2_standin"]["sentence"] + " More words.", [draft])
    [f] = corpus_findings(wd)["findings"]
    assert f["source"]["url"] == "file:other-author-draft.txt" and f["source"]["doi"] is None
    assert f["attribution"] == "unattributed" and f["source"]["found_via"] == "user_corpus"
    drive_strict(wd, tripwire)


def test_corpus_performance_budget(tmp_path):
    rnd = random.Random(1)
    vocab = [f"term{i}x" for i in range(4000)]

    def text(n):
        return " ".join(rnd.choice(vocab) for _ in range(n))

    d = tmp_path / "big"
    d.mkdir()
    for i in range(50):
        (d / f"p{i}.txt").write_text(text(10_000), "utf-8")
    doc_body = text(15_000)
    wd, out = prep(tmp_path, doc_body, [d])
    assert out["corpus"]["counts"] == {"loaded": 50}
    t0 = time.perf_counter()
    res = corpus_findings(wd)
    assert time.perf_counter() - t0 < 5.0
    assert len(res["sources_compared"]) == 50
    many = tmp_path / "tiny"
    many.mkdir()
    for i in range(200):
        (many / f"t{i}.txt").write_text(text(40), "utf-8")
    wd2 = tmp_path / "wd2"
    call_main(prepare.main, [str(write_md(tmp_path, "d2.md", "# I\n" + doc_body)), "--workdir", str(wd2), "--corpus", str(many)])
    t0 = time.perf_counter()
    corpus_findings(wd2)
    assert time.perf_counter() - t0 < 5.0
