import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "llm-tropes" / "scripts"))

from extract import Line  # noqa: E402
from patterns import Pattern, load_patterns  # noqa: E402
from scan import Finding, find_hits  # noqa: E402

GOOD = {
    "id": "neg-par",
    "category": "structural",
    "regex": r"\bit'?s not [^.]{1,40}\.\s+it'?s\b",
    "weight": 2.0,
    "source": "test",
    "explanation": "negative parallelism",
    "example": "It's not bold. It's backwards.",
}


def write(d, name, items):
    (d / name).write_text(json.dumps(items), encoding="utf-8")


def test_loader_rejects_pattern_missing_source(tmp_path):
    bad = {k: v for k, v in GOOD.items() if k != "source"}
    write(tmp_path, "p.json", [bad])
    with pytest.raises(ValueError, match="neg-par"):
        load_patterns(tmp_path)


def test_loader_rejects_bad_regex_names_id(tmp_path):
    write(tmp_path, "p.json", [{**GOOD, "regex": "(unclosed"}])
    with pytest.raises(ValueError, match="neg-par"):
        load_patterns(tmp_path)


def test_loader_accepts_null_regex_and_skips_wikipedia(tmp_path):
    write(tmp_path, "p.json", [{**GOOD, "regex": None}])
    write(tmp_path, "patterns_wikipedia.json", [{**GOOD, "id": "wiki"}])
    pats = load_patterns(tmp_path, include_wikipedia=False)
    assert [p.id for p in pats] == ["neg-par"]
    assert pats[0].regex is None and pats[0].contested is False
    assert len(load_patterns(tmp_path)) == 2
    assert find_hits([Line(1, "It's not bold. It's backwards.")], pats) == []


def _pats(tmp_path):
    write(tmp_path, "p.json", [GOOD])
    return load_patterns(tmp_path)


def test_hit_on_negative_parallelism(tmp_path):
    hits = find_hits([Line(1, "It's not bold. It's backwards.")], _pats(tmp_path))
    assert len(hits) == 1 and hits[0].category == "structural"


def test_no_hit_inside_code_block(tmp_path):
    ln = Line(1, "It's not bold. It's backwards.", in_code=True)
    assert find_hits([ln], _pats(tmp_path)) == []


def test_no_hit_inside_blockquote(tmp_path):
    ln = Line(1, "It's not bold. It's backwards.", in_quote=True)
    assert find_hits([ln], _pats(tmp_path)) == []


def test_finding_reports_matched_text_and_line(tmp_path):
    lines = [Line(1, "hello"), Line(2, "IT'S NOT BOLD. It's backwards.")]
    (h,) = find_hits(lines, _pats(tmp_path))
    assert h.line == 2 and h.pattern_id == "neg-par" and h.weight == 2.0
    assert h.text.lower().startswith("it's not bold") and h.contested is False
