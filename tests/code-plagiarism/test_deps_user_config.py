"""Strict tier: registry settings outside the project (environment variables, user-level npm/pip files)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"))

import deps  # noqa: E402

PRIV = "https://pypi.corp.example.net/simple"
NPM = {"package.json": json.dumps({"name": "x", "dependencies": {"left-pad": "1.3.0"}})}
PY = {"requirements.txt": "attrs==24.1.0\n"}


def sent(tmp_path, files):
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    plan = deps.make_plan(tmp_path)
    return sorted(c for r in plan["requests"] for c in json.loads(r["body"])), plan


def test_baseline_public_packages_are_sent(tmp_path):
    for sub, files in (("n", NPM), ("p", PY)):
        d = tmp_path / sub
        d.mkdir()
        names, _ = sent(d, files)
        assert names, sub


@pytest.mark.parametrize("var,value,files", [
    ("NPM_CONFIG_REGISTRY", "https://npm.corp.example.net/", NPM),
    ("npm_config_registry", "https://npm.corp.example.net/", NPM),
    ("PIP_INDEX_URL", PRIV, PY),
    ("PIP_EXTRA_INDEX_URL", PRIV, PY),
])
def test_private_registry_in_environment_withholds_names(tmp_path, monkeypatch, var, value, files):
    monkeypatch.setenv(var, value)
    names, plan = sent(tmp_path, files)
    assert names == []
    assert plan["excluded"]


def test_public_registry_in_environment_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://registry.npmjs.org/")
    names, _ = sent(tmp_path, NPM)
    assert names


def test_user_npmrc_private_registry_withholds_names(tmp_path, _isolated_user_config):
    (_isolated_user_config / ".npmrc").write_text("registry=https://npm.corp.example.net/\n", encoding="utf-8")
    names, _ = sent(tmp_path, NPM)
    assert names == []


def test_unreadable_user_npmrc_fails_closed(tmp_path, _isolated_user_config):
    (_isolated_user_config / ".npmrc").write_bytes(b"\xff\xfe\x00\x00 binary \x00")
    names, _ = sent(tmp_path, NPM)
    assert names == []


@pytest.mark.parametrize("rel", [".config/pip/pip.conf", ".pip/pip.conf", "pip/pip.ini"])
def test_user_pip_conf_private_index_withholds_names(tmp_path, _isolated_user_config, rel):
    f = _isolated_user_config / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"[global]\nindex-url = {PRIV}\n", encoding="utf-8")
    names, _ = sent(tmp_path, PY)
    assert names == []
