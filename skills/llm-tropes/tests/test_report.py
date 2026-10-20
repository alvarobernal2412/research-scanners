import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from scan import build_report  # noqa: E402

DATA = SCRIPTS.parent / "data"
SCAN_PY = SCRIPTS / "scan.py"

LEX = "It was a perfect storm of factors."
STRUCT = "Not a bug. Not a feature. A flaw."
CHAT = "Would you like me to expand on this?"
FILLER = "The committee reviewed the budget for the coming year and approved the schedule."


def md(tmp_path, text, name="doc.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def sec(report, i=0):
    return report["sections"][i]


def test_section_dense_when_three_categories(tmp_path):
    r = build_report(md(tmp_path, f"# A\n\n{LEX}\n\n{STRUCT}\n\n{CHAT}\n"), DATA)
    s = sec(r)
    assert s["heading"] == "# A" or s["heading"] == "A"
    assert len(s["categories"]) >= 3
    assert s["dense"] is True


def test_section_not_dense_with_two_categories(tmp_path):
    r = build_report(md(tmp_path, f"# A\n\n{LEX}\n\n{STRUCT}\n"), DATA)
    assert len(sec(r)["categories"]) == 2
    assert sec(r)["dense"] is False


def test_dense_ignores_contested_only_hits(tmp_path):
    r = build_report(md(tmp_path, "# A\n\nOne \u2014 two \u2014 three \u2014 four.\n"), DATA)
    s = sec(r)
    assert s["hit_count"] >= 3
    assert s["categories"] == []
    assert s["dense"] is False


def test_human_academic_fixture_not_dense(tmp_path):
    text = (
        "The survey of regional water districts covered forty municipalities over six years. "
        "However, response rates varied widely between the northern and southern counties. "
        "Moreover, the oldest records were kept on paper, so we digitised them by hand. "
        "Rainfall \u2014 which differed sharply from year to year \u2014 explained part of the variance in demand. "
        "Households with metered supply reduced consumption by roughly eight percent after the tariff change, "
        "whereas unmetered households showed no measurable response. "
        "Industrial users behaved differently \u2014 their consumption tracked production schedules rather than price. "
        "We therefore analyse the two groups separately and report confidence intervals for each estimate. "
        "The limitations of the data are discussed in the final section, together with directions for further work."
    )
    r = build_report(md(tmp_path, text), DATA, "academic")
    assert all(not s["dense"] for s in r["sections"])
    assert r["stats"]["words"] > 100


def test_all_code_markdown_zero_findings_exit_0(tmp_path):
    p = md(tmp_path, "```\nperfect storm\n```\n")
    res = subprocess.run([sys.executable, "-I", str(SCAN_PY), str(p)], capture_output=True)
    assert res.returncode == 0
    out = json.loads(res.stdout.decode("utf-8"))
    assert out["findings"] == []
    assert out["error"] is None


def test_non_english_text_sets_language_note_no_findings(tmp_path):
    text = (
        "El comit\u00e9 revis\u00f3 el presupuesto anual y aprob\u00f3 el calendario de actividades para los pr\u00f3ximos meses. "
        "Los participantes discutieron las propuestas con calma, y despu\u00e9s votaron por mayor\u00eda."
    )
    r = build_report(md(tmp_path, text), DATA)
    assert r["language_note"]
    assert r["findings"] == []


def test_cli_prints_valid_json_with_disclaimer(tmp_path):
    p = md(tmp_path, f"{FILLER}\n\n{LEX}\n")
    res = subprocess.run([sys.executable, "-I", str(SCAN_PY), str(p)], capture_output=True)
    assert res.returncode == 0
    out = json.loads(res.stdout.decode("utf-8"))
    for k in ("file", "profile", "findings", "sections", "stats", "density_per_1k",
              "language_note", "disclaimer", "error"):
        assert k in out
    assert "authorship" in out["disclaimer"]
    assert out["density_per_1k"] > 0


def test_cli_no_text_pdf_exits_2_with_error_code(tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF\n")
    res = subprocess.run([sys.executable, "-I", str(SCAN_PY), str(p)], capture_output=True)
    assert res.returncode == 2
    assert "error" in json.loads(res.stdout.decode("utf-8"))


def test_profile_filters_patterns(tmp_path):
    d = tmp_path / "pats"
    d.mkdir()
    base = {"category": "lexical", "weight": 1.0, "source": "t", "explanation": "x", "example": "x"}
    (d / "p.json").write_text(json.dumps([
        {**base, "id": "all", "regex": r"\bfoo\b"},
        {**base, "id": "acad", "regex": r"\bbar\b", "profiles": ["academic"]},
    ]), encoding="utf-8")
    f = md(tmp_path, "foo and bar here.\n")
    ids = lambda prof: {x["pattern_id"] for x in build_report(f, d, prof)["findings"]}
    assert ids("academic") == {"all", "acad"}
    assert ids("general") == {"all"}
