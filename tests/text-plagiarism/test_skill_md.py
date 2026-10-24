import re
from pathlib import Path

from conftest import ROOT, SCRIPTS

SKILL = ROOT / "skills" / "text-plagiarism"
TEXT = (SKILL / "SKILL.md").read_text(encoding="utf-8")


def frontmatter():
    m = re.match(r"^---\n(.*?)\n---\n", TEXT, re.S)
    assert m, "SKILL.md must start with frontmatter"
    return dict(re.findall(r"^(\w+):\s*(.*)$", m.group(1), re.M))


def test_frontmatter_name_and_description():
    fm = frontmatter()
    assert fm["name"] == "text-plagiarism" and fm["license"] == "Apache-2.0"
    desc = fm["description"].lower()
    for needle in ("plagiarism", "copied", "unattributed", "quotation", "open-access", "coverage"):
        assert needle in desc, needle
    assert "metadata" not in fm and "under development" not in desc          # no longer the hidden scaffold


def test_every_cli_mentioned_exists():
    named = set(re.findall(r"scripts/([a-z_]+\.py)", TEXT))
    assert {"prepare.py", "strict.py", "open_tier.py", "report.py", "approval.py", "corpus.py", "evaluate.py"} <= named
    for name in named:
        assert (SCRIPTS / name).is_file(), name
    for ref in re.findall(r"`(references/[a-z\-]+\.md)`", TEXT):
        assert (SKILL / ref).is_file(), ref


def test_skill_md_states_required_sentences():
    flat = " ".join(TEXT.split())
    assert "The scripts cannot tell the user from Claude." in flat
    assert "The guarantee covers only requests made by the skill's scripts" in flat
    assert re.search(r"Never present a no-finding result as \"clean\"", flat)
    assert "Never give a legal verdict" in flat
    assert "permission prompt" in flat and "explicit yes" in flat


def test_body_under_500_lines():
    assert len(TEXT.splitlines()) < 500


def test_skill_folder_is_self_contained_and_runtime_only():
    reqs = (SKILL / "requirements.txt").read_text(encoding="utf-8").split()
    assert set(reqs) == {"pypdf", "python-docx"}
    assert not (SKILL / "tests").exists()
    for script in SCRIPTS.glob("*.py"):
        src = script.read_text(encoding="utf-8")
        assert "sys.path.insert(0, str(Path(__file__).resolve().parent))" in src, script.name
        for other in ("llm_tropes", "code_plagiarism", "reference_check", "license_compliance"):
            assert other not in src, (script.name, other)


def test_readme_has_pipeline_diagram_and_limitations():
    readme = (SKILL / "README.md").read_text(encoding="utf-8")
    assert "## Pipeline" in readme and "```mermaid" in readme and "flowchart TD" in readme
    assert "## Limitations" in readme and "Apache-2.0" in readme
    blocks = re.findall(r"```mermaid\n(.*?)```", readme, re.S)
    assert blocks and blocks[0].count("[") == blocks[0].count("]") and blocks[0].count("(") == blocks[0].count(")")
