import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "llm-tropes" / "scripts"))

from patterns import load_patterns  # noqa: E402

DATA = Path(__file__).resolve().parents[2] / "skills" / "llm-tropes" / "data"
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
        # whole-name match on word boundaries; fails if a source is not defined in ATTRIBUTION.md
        assert re.search(r"(?<![\w/])" + re.escape(n) + r"(?![\w])", text), f"{n} not defined in ATTRIBUTION.md"
    assert "CC BY-SA 4.0" in text and "MIT" in text


def test_conclusion_and_transition_fire_at_line_start_only_where_expected():
    from extract import Line
    from scan import find_hits

    doc = [
        "# Results",
        "In conclusion, we recommend X.",
        "",
        "## Discussion",
        "Moreover, the results hold.",
        "We said that in conclusion, nothing changed.",
    ]
    lines = [Line(no=i + 1, text=t) for i, t in enumerate(doc)]
    hits = {(f.pattern_id, f.line) for f in find_hits(lines, load_patterns(DATA))}
    assert ("signposted-conclusion", 2) in hits
    assert ("filler-transitions", 5) in hits
    assert not any(pid == "signposted-conclusion" and ln == 6 for pid, ln in hits)


def test_example_matches_its_own_regex():
    from extract import Line
    from scan import find_hits

    pats = load_patterns(DATA)
    for p in pats:
        if p.regex is None:
            continue
        assert "\n" not in p.example, f"{p.id}: example must be a single line (scan is per line)"
        hits = find_hits([Line(1, p.example)], pats)
        assert p.id in {h.pattern_id for h in hits}, p.id


def test_em_dash_pattern_contested_and_low_weight():
    em = [p for p in load_patterns(DATA) if "em-dash" in p.id]
    assert em
    for p in em:
        assert p.contested and p.weight <= 0.25 and p.category == "formatting"


# ---- final-review regressions -------------------------------------------------

def _neg_par():
    return next(p for p in load_patterns(DATA) if p.id == "negative-parallelism")


def test_negative_parallelism_recall():
    rx = re.compile(_neg_par().regex, re.IGNORECASE)
    for s in (
        "AI isn't just a trend — it's a fundamental shift",
        "Agents aren't just chatbots—they're coworkers",
        "It's not bold. It's backwards.",
        "AI isn’t just a trend — it’s a fundamental shift",
        "It’s not bold. It’s backwards.",
    ):
        assert rx.search(s), s


def test_negative_parallelism_human_negatives():
    rx = re.compile(_neg_par().regex, re.IGNORECASE)
    for s in (
        "The tool isn't perfect, but it works.",
        "We did not measure pH.",
        "The results are not significant at the five percent level.",
        "This is not the first time the committee has met in March.",
    ):
        assert not rx.search(s), s


def test_chatbot_placeholder_ignores_citations():
    from extract import Line
    from scan import find_hits

    pats = [p for p in load_patterns(DATA) if p.id == "chatbot-placeholder-text"]
    for s in ("See [Yourdon 1979] for details.", "Cited as [Date, 2019].", "In [Name of journal] it appeared."):
        assert find_hits([Line(1, s)], pats) == [], s
    for s in ("Dear [Your Name],", "Dear [Insert Name], thanks", "At [Company Name] we", "Dated [Date] today", "Hi [Name]."):
        assert find_hits([Line(1, s)], pats), s
