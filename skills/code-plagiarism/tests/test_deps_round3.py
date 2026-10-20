"""Strict tier, fix round 3: pyproject registries reach requirements files; sticky private index."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import approval  # noqa: E402
import deps  # noqa: E402
from inventory import inventory  # noqa: E402

NL = "\n"
ATTRS = ["pypi/pypi/-/attrs/24.1.0"]
REQ = "attrs==24.1.0" + NL
needs_toml = pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib needs Python 3.11+")


def mk(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8", newline="")
    return tmp_path


def sent(tmp_path, files):
    mk(tmp_path, files)
    plan = deps.make_plan(tmp_path)
    return sorted(c for r in plan["requests"] for c in json.loads(r["body"]))


def reasons_of(tmp_path):
    out = {}
    for e in deps.make_plan(tmp_path)["excluded"]:
        m = re.fullmatch(r"(\w+) \((\d+)\)", e["reason"])
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
    return out


PRIV = "https://u:SECRET@pypi.corp.internal/simple"
VARIANTS = {
    "uv.pip index-url": ('[tool.uv.pip]\nindex-url="%s"\n' % PRIV, "private_registry"),
    "uv index-url": ('[tool.uv]\nindex-url="%s"\n' % PRIV, "private_registry"),
    "uv index table": ('[[tool.uv.index]]\nname="x"\nurl="%s"\n' % PRIV, "private_registry_possible"),
    "uv find-links": ('[tool.uv]\nfind-links=["./wheels"]\n', "private_registry_possible"),
    "uv.pip extra-index": ('[tool.uv.pip]\nextra-index-url=["%s"]\n' % PRIV, "private_registry_possible"),
    "poetry source": ('[[tool.poetry.source]]\nname="c"\nurl="%s"\n' % PRIV, "private_registry_possible"),
}


@needs_toml
@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_pyproject_registry_reaches_sibling_requirements(tmp_path, name):
    text, reason = VARIANTS[name]
    assert sent(tmp_path, {"requirements.txt": REQ, "pyproject.toml": "[project]\nname='x'\n" + text}) == []
    assert reasons_of(tmp_path) == {reason: 1}
    plan = deps.make_plan(tmp_path)
    assert "SECRET" not in json.dumps(plan) + approval.render_preview(plan)


@needs_toml
@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_pyproject_registry_reaches_nested_requirements(tmp_path, name):
    text, reason = VARIANTS[name]
    assert sent(tmp_path, {"sub/deep/requirements.txt": REQ, "pyproject.toml": "[project]\nname='x'\n" + text}) == []
    assert reasons_of(tmp_path) == {reason: 1}


@needs_toml
def test_pyproject_registry_reaches_other_pyproject_dependency_lists(tmp_path):
    files = {"pyproject.toml": "[project]\nname='x'\n" + VARIANTS["uv.pip index-url"][0],
             "sub/pyproject.toml": "[project]\ndependencies=['attrs==24.1.0']\n"}
    assert sent(tmp_path, files) == []


@needs_toml
def test_public_only_pyproject_leaves_requirements_public(tmp_path):
    assert sent(tmp_path / "a", {"requirements.txt": REQ, "pyproject.toml":
                                 '[tool.uv.pip]\nindex-url="https://pypi.org/simple"\n'}) == ATTRS
    assert sent(tmp_path / "b", {"requirements.txt": REQ, "pyproject.toml":
                                 '[[tool.uv.index]]\nname="pypi"\nurl="https://pypi.org/simple"\n'}) == ATTRS
    assert sent(tmp_path / "c", {"requirements.txt": REQ, "pyproject.toml": "[project]\nname='x'\n"}) == ATTRS


@needs_toml
def test_later_public_index_url_does_not_reset_pyproject_private(tmp_path):
    text = VARIANTS["uv.pip index-url"][0]
    assert sent(tmp_path, {"requirements.txt": "--index-url https://pypi.org/simple" + NL + REQ,
                           "pyproject.toml": text}) == []


@needs_toml
def test_unreadable_pyproject_fails_closed_for_requirements(tmp_path):
    assert sent(tmp_path / "a", {"requirements.txt": REQ, "pyproject.toml": "[project\nnot toml"}) == []
    r = reasons_of(tmp_path / "a")
    assert r.get("config_unreadable") and r.get("private_registry_possible") == 1
    pad = "# " + "x" * 300_000 + NL + "[project]\nname='x'\n"
    assert sent(tmp_path / "b", {"requirements.txt": REQ, "pyproject.toml": pad}) == []
    r = reasons_of(tmp_path / "b")
    assert r.get("config_unreadable") and r.get("private_registry_possible") == 1
    assert sent(tmp_path / "c", {"requirements.txt": REQ, "pyproject.toml": b"\xff\xfe\x00\x00bad"}) == []


# ---- sticky private index --------------------------------------------------------------

def test_private_then_public_index_url_in_one_file_stays_private(tmp_path):
    text = ("--index-url https://pypi.corp.internal/simple" + NL +
            "--index-url https://pypi.org/simple" + NL + REQ)
    assert sent(tmp_path, {"requirements.txt": text}) == []
    assert reasons_of(tmp_path) == {"private_registry": 1}


def test_private_then_public_across_include_stays_private(tmp_path):
    files = {"requirements.txt": "-i https://pypi.corp.internal/simple" + NL + "-r more.txt" + NL,
             "more.txt": "--index-url https://pypi.org/simple" + NL + REQ}
    assert sent(tmp_path, files) == []
    assert sent(tmp_path / "w", {"requirements.txt": "-r more.txt" + NL + "-i https://pypi.org/simple" + NL,
                                 "more.txt": "-i https://pypi.corp.internal/simple" + NL + REQ}) == []


def test_public_then_private_still_private_and_public_only_still_public(tmp_path):
    assert sent(tmp_path / "a", {"requirements.txt": "-i https://pypi.org/simple" + NL +
                                 "-i https://pypi.corp.internal/simple" + NL + REQ}) == []
    assert sent(tmp_path / "b", {"requirements.txt": "-i https://pypi.org/simple" + NL + REQ}) == ATTRS


def test_pip_conf_private_then_public_requirements_option(tmp_path):
    assert sent(tmp_path, {"pip.conf": "[global]\nindex-url = https://pypi.corp.internal/simple\n",
                           "requirements.txt": "-i https://pypi.org/simple" + NL + REQ}) == []


# ---- pnpm workspace files ----------------------------------------------------------------

ISODD = json.dumps({"dependencies": {"is-odd": "3.0.1"}})


@pytest.mark.parametrize("fname", ["pnpm-workspace.yaml", ".pnpmfile.cjs"])
def test_pnpm_files_are_unsupported_and_mark_npm_possible(tmp_path, fname):
    assert sent(tmp_path, {"package.json": ISODD, fname: "packages:\n  - 'x/*'\n", "main.js": "1"}) == []
    r = reasons_of(tmp_path)
    assert r.get("private_registry_possible") == 1 and r.get("unsupported_manifest") == 1


def test_pnpm_file_at_root_applies_to_nested_package(tmp_path):
    assert sent(tmp_path, {"pnpm-workspace.yaml": "packages:\n  - 'p/*'\n", "p/a/package.json": ISODD}) == []


def test_pnpm_note_present_in_results(tmp_path):
    mk(tmp_path, {"package.json": ISODD, "pnpm-workspace.yaml": "packages: []\n"})
    plan = deps.make_plan(tmp_path)
    pp = tmp_path / "p.json"
    approval.write_plan(plan, pp)
    res = deps.run(pp, plan["sha256"], opener=lambda *a, **k: 1 / 0)
    assert any("pnpm" in n for n in res["notes"])


# ---- setup.cfg and Pipfile ----------------------------------------------------------------

@pytest.mark.parametrize("cfg", [
    "[easy_install]\nindex-url = https://u:SECRET@pypi.corp.internal/simple\n",
    "[options]\nindex_url = https://pypi.corp.internal/simple\n",
    "[easy_install]\nindex_url=https://pypi.corp.internal/simple\n"])
def test_setup_cfg_index_marks_requirements_possible(tmp_path, cfg):
    assert sent(tmp_path, {"requirements.txt": REQ, "setup.cfg": cfg}) == []
    assert reasons_of(tmp_path).get("private_registry_possible") == 1
    assert "SECRET" not in json.dumps(deps.make_plan(tmp_path))


def test_setup_cfg_without_index_changes_nothing(tmp_path):
    assert sent(tmp_path, {"requirements.txt": REQ, "setup.cfg": "[metadata]\nname = x\n"}) == ATTRS
    assert sent(tmp_path / "w", {"requirements.txt": REQ, "setup.cfg":
                                 "[easy_install]\nindex-url = https://pypi.org/simple\n"}) == ATTRS


@needs_toml
def test_pipfile_custom_source_marks_requirements_possible(tmp_path):
    pf = '[[source]]\nurl = "https://u:SECRET@pypi.corp.internal/simple"\nverify_ssl = true\nname = "corp"\n'
    assert sent(tmp_path, {"sub/requirements.txt": REQ, "Pipfile": pf}) == []
    assert reasons_of(tmp_path).get("private_registry_possible") == 1
    assert "SECRET" not in json.dumps(deps.make_plan(tmp_path))


@needs_toml
def test_pipfile_pypi_source_and_unreadable_pipfile(tmp_path):
    pub = '[[source]]\nurl = "https://pypi.org/simple"\nverify_ssl = true\nname = "pypi"\n'
    assert sent(tmp_path / "a", {"requirements.txt": REQ, "Pipfile": pub}) == ATTRS
    assert sent(tmp_path / "b", {"requirements.txt": REQ, "Pipfile": "[[source\nbroken"}) == []
    assert reasons_of(tmp_path / "b").get("config_unreadable")
    assert sent(tmp_path / "c", {"requirements.txt": REQ, "setup.cfg": b"\x00\x01binary"}) == []
    assert reasons_of(tmp_path / "c").get("config_unreadable")


def test_inventory_still_lists_config_files_as_unsupported(tmp_path):
    out = deps.read_dependencies(inventory(mk(tmp_path, {"Pipfile": "x", "setup.cfg": "y"}), require_sources=False))
    assert {d.manifest for d in out if d.excluded_reason == "unsupported_manifest"} >= {"Pipfile", "setup.cfg"}
