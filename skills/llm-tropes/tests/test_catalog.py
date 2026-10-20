import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from patterns import load_patterns  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
CATEGORIES = {"lexical", "structural", "rhetorical", "document", "formatting", "chatbot_residue"}


def source_names(source):
    # Convention: `source` is "<Name> — <detail/URL>" (optionally several joined by "; ").
    # The short name is the text before " — "; ATTRIBUTION.md must contain every such name.
    return [part.split(" — ")[0].strip() for part in source.split("; ")]


def test_catalog_loads_without_error():
    pats = load_patterns(DATA)
    assert len(pats) >= 40
    assert all(p.category in CATEGORIES for p in pats)


def test_every_pattern_has_source_url_or_name():
    for p in load_patterns(DATA):
        assert p.source.strip() and " — " in p.source, p.id


def test_ids_unique():
    ids = [p.id for p in load_patterns(DATA)]
    assert len(ids) == len(set(ids))


def test_wikipedia_entries_only_in_wikipedia_file():
    plain = load_patterns(DATA, include_wikipedia=False)
    assert plain and not any("Wikipedia" in p.source for p in plain)
    wiki_ids = {p.id for p in load_patterns(DATA)} - {p.id for p in plain}
    assert wiki_ids
    for p in load_patterns(DATA):
        if p.id in wiki_ids:
            assert "Wikipedia" in p.source and "wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing" in p.source, p.id


def test_attribution_file_names_every_source_used():
    text = (DATA / "ATTRIBUTION.md").read_text(encoding="utf-8")
    names = {n for p in load_patterns(DATA) for n in source_names(p.source)}
    assert names
    for n in names:
        assert n in text, n
    assert "CC BY-SA 4.0" in text and "MIT" in text


def test_example_matches_its_own_regex():
    for p in load_patterns(DATA):
        if p.regex is None:
            continue
        assert re.search(p.regex, p.example, re.IGNORECASE), p.id


def test_em_dash_pattern_contested_and_low_weight():
    em = [p for p in load_patterns(DATA) if "em-dash" in p.id]
    assert em
    for p in em:
        assert p.contested and p.weight <= 0.25 and p.category == "formatting"
