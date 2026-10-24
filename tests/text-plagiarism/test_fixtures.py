import base64
import json
import re
from pathlib import Path
from urllib.parse import unquote

from extract import extract_bytes

FX = Path(__file__).resolve().parent / "fixtures"
RESP = FX / "responses"


def _load(name):
    return json.loads((RESP / name).read_text(encoding="utf-8"))


def _body(name):
    return base64.b64decode(_load(name)["body_b64"])


def _squash(text):
    return re.sub(r"[\s\-­]+", "", text).casefold()


def test_index_files_exist_and_parse():
    index = json.loads((RESP / "index.json").read_text(encoding="utf-8"))
    assert len(index) >= 15
    for url, name in index.items():
        assert url.startswith("https://")
        assert (RESP / name).is_file(), name
        assert _load(name)


def test_every_response_has_url_status_body():
    for p in RESP.glob("*.json"):
        if p.name == "index.json":
            continue
        rec = json.loads(p.read_text(encoding="utf-8"))
        assert rec["url"].startswith("https://") and rec["method"] == "GET"
        assert isinstance(rec["status"], int) and "body_b64" in rec and isinstance(rec["headers"], dict)


def test_passage_occurs_in_recorded_fulltext():
    passages = json.loads((FX / "passages.json").read_text(encoding="utf-8"))
    doc = extract_bytes(_body("pdf-A.json"), "pdf")
    text = _squash(" ".join(l.text for l in doc.lines))
    passage = passages["workA"]["passage"]
    assert len(passage.split()) >= 30
    assert _squash(passage) in text
    assert _squash(passages["workA"]["control_sentence"]) in text
    xml = _body("epmc-fulltext-D.json").decode("utf-8")
    flat = _squash(re.sub(r"<[^>]+>", " ", xml))
    for s in passages["pmc"]["sentences"]:
        assert _squash(s) in flat


def test_provenance_lists_every_doi_with_license():
    prov = (FX / "PROVENANCE.md").read_text(encoding="utf-8")
    passages = json.loads((FX / "passages.json").read_text(encoding="utf-8"))
    for doi in (passages["workA"]["doi"], passages["pmc"]["doi"], passages["apostrophe"]["doi"]):
        assert doi in prov
    assert "CC BY 4.0" in prov


def test_no_recorded_url_contains_t2_standin():
    passages = json.loads((FX / "passages.json").read_text(encoding="utf-8"))
    needle = " ".join(passages["t2_standin"]["sentence"].lower().split()[:6])
    for p in RESP.glob("*.json"):
        assert needle not in unquote(p.read_text(encoding="utf-8")).lower()


def test_fixture_dir_under_5_mb():
    total = sum(f.stat().st_size for f in FX.rglob("*") if f.is_file())
    assert total < 5_000_000
