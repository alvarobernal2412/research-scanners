"""Helper programs (git, scancode) are never taken from the cwd or the scan root."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"))

import headers  # noqa: E402
import inventory  # noqa: E402
import model  # noqa: E402


def _plant(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    names = [name + ".exe", name + ".bat"] if os.name == "nt" else [name]
    for n in names:
        f = directory / n
        f.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(f, 0o755)
    return directory / names[0]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    cwd, root, real = tmp_path / "cwd", tmp_path / "root", tmp_path / "real"
    for n in ("git", "scancode"):
        _plant(cwd, n)
        _plant(root, n)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(cwd), str(root)]))
    return root, real


@pytest.mark.parametrize("name", ["git", "scancode"])
def test_find_exe_ignores_cwd_and_root(env, name):
    root, _real = env
    assert model.find_exe(name, root) is None


def test_find_exe_uses_a_real_path_entry(env, monkeypatch):
    root, real = env
    good = _plant(real, "scancode")
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(real)]))
    assert Path(model.find_exe("scancode", root)).resolve() == good.resolve()


def test_scancode_in_scanned_repo_is_not_available(env):
    root, _ = env
    assert headers.scancode_status(root) == "not_installed"
    assert headers.run_scancode(root, ["a.py"]) is None


def test_git_in_scanned_repo_is_not_run(env, monkeypatch):
    root, _ = env
    (root / ".git").mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(inventory.subprocess, "run", lambda *a, **k: calls.append(a) or pytest.fail("ran"))
    assert inventory._git_lists(root) is None
    assert not calls
