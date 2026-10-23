import json

import deps


def names(res, **flt):
    return {(d["name"], d["version"]) for d in res["dependencies"]
            if all(d[k] == v for k, v in flt.items())}


def test_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "requests==2.31.0\nflask>=2.0  # web\n-e git+https://github.com/o/r.git#egg=priv\n"
        "./local/pkg\nnumpy==1.26.0 ; python_version>'3.8'\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("-r requirements.txt\npytest==8.0.0\n", encoding="utf-8")
    res = deps.collect(tmp_path)
    runtime = names(res, scope="runtime")
    assert ("requests", "2.31.0") in runtime and ("numpy", "1.26.0") in runtime
    assert ("flask", None) in runtime
    assert ("pytest", "8.0.0") in names(res, scope="dev")
    sources = {d["name"]: d["source"] for d in res["dependencies"]}
    assert sources["priv"] == "git"
    assert sources["./local/pkg"] == "path" or any(d["source"] == "path" for d in res["dependencies"])
    assert any("transitive" in n for n in res["notes"])  # flask is unpinned


def test_fully_pinned_requirements_no_note(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    assert deps.collect(tmp_path)["notes"] == []


def test_pyproject_pep621_and_extras(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\ndependencies=["click>=8", "rich==13.0.0"]\n'
        '[project.optional-dependencies]\ndev=["pytest"]\nplots=["matplotlib"]\n', encoding="utf-8")
    res = deps.collect(tmp_path)
    scopes = {d["name"]: d["scope"] for d in res["dependencies"]}
    assert scopes["click"] == "runtime" and scopes["pytest"] == "dev" and scopes["matplotlib"] == "runtime"


def test_pyproject_poetry(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython="^3.10"\nhttpx="0.27.0"\nprivate={git="https://x/y.git"}\n'
        '[tool.poetry.group.dev.dependencies]\nblack="*"\n', encoding="utf-8")
    res = deps.collect(tmp_path)
    by = {d["name"]: d for d in res["dependencies"]}
    assert "python" not in by
    assert by["httpx"]["version"] == "0.27.0" and by["private"]["source"] == "git"
    assert by["black"]["scope"] == "dev"


def test_package_json_and_lock_v3(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"left-pad": "^1.3.0", "mine": "github:me/mine", "loc": "file:../loc"},
        "devDependencies": {"jest": "^29.0.0"}}), encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "root"},
        "node_modules/left-pad": {"version": "1.3.0", "license": "WTFPL"},
        "node_modules/jest": {"version": "29.7.0", "dev": True, "license": "MIT"},
        "node_modules/jest/node_modules/inner": {"version": "1.0.0", "dev": True},
        "node_modules/mine": {"version": "1.0.0", "resolved": "git+ssh://git@github.com/me/mine.git"},
    }}), encoding="utf-8")
    res = deps.collect(tmp_path)
    by = {(d["name"], d["version"]): d for d in res["dependencies"]}
    assert by[("left-pad", "1.3.0")]["direct"] and by[("left-pad", "1.3.0")]["license_hint"] == "WTFPL"
    assert by[("jest", "29.7.0")]["scope"] == "dev"
    assert not by[("inner", "1.0.0")]["direct"]
    assert by[("mine", "1.0.0")]["source"] == "git"
    assert ("left-pad", None) not in by  # unpinned manifest entry replaced by the lock
    assert res["notes"] == []


def test_skips_node_modules_and_empty(tmp_path):
    nm = tmp_path / "node_modules" / "a"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text(json.dumps({"dependencies": {"zzz": "1.0.0"}}), encoding="utf-8")
    res = deps.collect(tmp_path)
    assert res["dependencies"] == [] and res["manifests"] == []


def test_pipfile_and_poetry_lock(tmp_path):
    (tmp_path / "Pipfile.lock").write_text(json.dumps({
        "default": {"requests": {"version": "==2.31.0"}}, "develop": {"pytest": {"version": "==8.0.0"}}}),
        encoding="utf-8")
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "idna"\nversion = "3.4"\ngroups = ["main"]\n\n'
        '[[package]]\nname = "mypy"\nversion = "1.0"\ngroups = ["dev"]\n', encoding="utf-8")
    res = deps.collect(tmp_path)
    by = {d["name"]: d for d in res["dependencies"]}
    assert by["requests"]["scope"] == "runtime" and by["pytest"]["scope"] == "dev"
    assert by["idna"]["scope"] == "runtime" and by["mypy"]["scope"] == "dev"
