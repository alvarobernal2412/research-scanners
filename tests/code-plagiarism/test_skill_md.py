"""SKILL.md, README and reference guides: structure, required statements, and agreement with the scripts."""
import json
import re
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism"
sys.path.insert(0, str(SKILL / "scripts"))

import report  # noqa: E402
import scanoss_backend as sb  # noqa: E402
import stackexchange as se  # noqa: E402

TEXT = (SKILL / "SKILL.md").read_text(encoding="utf-8")
README = (SKILL / "README.md").read_text(encoding="utf-8")
DOCS = {p.name: p.read_text(encoding="utf-8") for p in (SKILL / "references").glob("*.md")}


def frontmatter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "missing frontmatter"
    out = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def test_frontmatter():
    fm = frontmatter(TEXT)
    assert fm["name"] == "code-plagiarism"
    assert fm["license"] == "Apache-2.0"
    desc = fm["description"]
    assert 0 < len(desc) <= 1024
    low = desc.lower()
    assert "stack overflow" in low and "licen" in low and "attribution" in low
    assert "legal verdict" in low


def test_required_statements():
    low = TEXT.lower()
    for needle in ("cannot tell", "permission prompt", "already in the model's context", "--sha256",
                   "all rights reserved", "if copyrightable"):
        assert needle in low, needle


def test_hard_gates_are_stated():
    low = TEXT.lower()
    assert "never say the code is \"clean\"" in low
    assert "no matches found in the checked scope" in low
    assert "who copied from whom" in low
    assert "never state or imply who wrote the code" in low
    assert "never run a `run` command without an explicit yes" in low


def test_every_script_referenced_exists():
    names = set()
    for text in [TEXT, README, *DOCS.values()]:
        names |= set(re.findall(r"scripts/([A-Za-z_]+\.py)", text))
    assert {"inventory.py", "report.py", "deps.py", "scanoss_backend.py", "stackexchange.py",
            "approval.py"} <= names
    for n in names:
        assert (SKILL / "scripts" / n).is_file(), n


def test_every_reference_file_linked_exists():
    for text in (TEXT, README):
        for rel in re.findall(r"references/([A-Za-z0-9_.-]+\.md)", text):
            assert (SKILL / "references" / rel).is_file(), rel
    assert {"privacy.md", "licensing.md", "report-format.md"} <= set(DOCS)


def test_every_documented_flag_exists_in_a_script():
    source = "\n".join(p.read_text(encoding="utf-8") for p in (SKILL / "scripts").glob("*.py"))
    flags = set(re.findall(r"`[^`\n]*?(--[a-z][a-z0-9-]+)", TEXT + README + "".join(DOCS.values())))
    flags |= set(re.findall(r"(--[a-z][a-z0-9-]+)", re.sub(r"```.*?```", "", TEXT, flags=re.S)))
    flags -= {"--root"} if "--root" in source else set()
    for flag in flags:
        assert f'"{flag}"' in source, flag


def test_every_script_command_uses_isolated_python():
    cmds = re.findall(r"python (?:-I )?[^\n`]*scripts/[A-Za-z_<>/]+\.py", TEXT)
    assert cmds
    assert all(c.startswith("python -I ") for c in cmds)


def test_commands_in_skill_md_match_the_clis():
    for needle in ("scripts/deps.py plan <root> --out", "scripts/scanoss_backend.py plan <root> --include",
                   "scripts/stackexchange.py plan <root> --include", "--root <root> --out <results.json>",
                   "approval.py strike <plan.json> --ids", "report.py <root> --results"):
        assert needle in TEXT, needle


def test_privacy_guide_carries_the_disclosures_verbatim():
    privacy = DOCS["privacy.md"]
    for line in sb.DISCLOSURE + se.DISCLOSURE:
        assert line in privacy, line[:60]
    for needle in ("300 requests/day", "5000 files/day", "STACKEXCHANGE_KEY", "--use-key"):
        assert needle in privacy


def test_report_format_guide_has_the_table_and_disclaimer():
    fmt = DOCS["report-format.md"]
    assert "| File:lines | Match | Candidate origins (url, author, date) | Licence (evidence, confidence) | " \
           "Attribution | Obligation |" in fmt
    assert report.DISCLAIMER in fmt


def test_licensing_guide_covers_the_rules():
    low = DOCS["licensing.md"].lower()
    for needle in ("per revision", "2011-04-08", "2018-05-02", "all rights are reserved", "if copyrightable",
                   "per-file header", "not origins"):
        assert needle in low, needle


def test_shipped_wording_has_no_verdict_words():
    data = (SKILL / "data" / "obligations.json").read_text(encoding="utf-8")
    for text in (data, report.DISCLAIMER, report.DIRECTION_NOTE, *sb.DISCLOSURE, *se.DISCLOSURE):
        low = text.lower()
        for bad in ("infring", "violat", "illegal", "guilty", "stolen"):
            assert bad not in low, bad


def test_readme_sections_and_cases():
    for heading in ("## What it is for, and what it is not for", "## Use cases", "## How to use", "## Pipeline",
                    "## Privacy", "## How to read a finding", "## Limitations", "## Quality checks", "## License"):
        assert heading in README, heading
    for case in ("C1", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10"):
        assert f"({case}" in README, case
    assert "Apache-2.0" in README


MERMAID = re.search(r"```mermaid\n(.*?)\n```", README, re.S)


def test_readme_mermaid_block_is_wellformed():
    assert MERMAID, "no mermaid block"
    src = MERMAID.group(1)
    assert src.startswith("flowchart TD")
    assert src.count('"') % 2 == 0
    for line in src.splitlines():
        assert line.count('"') % 2 == 0, line
    assert len(re.findall(r"^\s*subgraph\b", src, re.M)) == len(re.findall(r"^\s*end\s*$", src, re.M))
    ids_defined = set(re.findall(r"(?:^|\s|>)([A-Za-z][A-Za-z0-9]*)(?=[\[\(\{]\"|\(\[\")", src))
    used = set()
    for line in src.splitlines():
        if "-->" in line or "-.->" in line or "-.-" in line:
            for part in re.split(r"-\.?->(?:\|\"[^\"]*\"\|)?", line):
                m = re.match(r"\s*([A-Za-z][A-Za-z0-9]*)", part)
                if m:
                    used.add(m.group(1))
    assert used <= ids_defined, used - ids_defined
    # every network step is a dashed edge labelled NETWORK, and sits behind an approval gate
    assert src.count("NETWORK") == 3 and src.count("-.->|\"NETWORK") == 3
    assert src.count("You approve this") == 3 and "sha256" in src


def test_install_wording_matches_requirements_and_error_keys():
    import subprocess
    assert "installs the test runner" not in TEXT
    out = subprocess.run([sys.executable, "-I", str(SKILL / "scripts" / "inventory.py")],
                         capture_output=True, text=True).stdout
    err = json.loads(out)
    assert "detail" in err and "message" not in err
