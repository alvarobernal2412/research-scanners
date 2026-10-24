"""Strict tier, fix round 1: private registries, scope grouping, coverage gaps, status mapping."""
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"))

import approval  # noqa: E402
import deps  # noqa: E402
from deps import Dependency  # noqa: E402
from inventory import InventoryError, inventory  # noqa: E402


def mk(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8", newline="")
    return tmp_path


def read(tmp_path, files):
    mk(tmp_path, files)
    return deps.read_dependencies(inventory(tmp_path, require_sources=False))


def _timed(fn):
    t = time.perf_counter()
    r = fn()
    assert time.perf_counter() - t < 2.0
    return r


class OkOpener:
    def __init__(self, body):
        self.body = body

    def open(self, req, timeout=None):
        body = self.body
        return type("R", (), {"status": 200, "headers": {}, "read": lambda s, n=-1: body,
                              "getcode": lambda s: 200, "close": lambda s: None})()

    __call__ = open


def _plan_file(tmp_path, files):
    mk(tmp_path / "proj", files)
    plan = deps.make_plan(tmp_path / "proj")
    pp = tmp_path / "plan.json"
    approval.write_plan(plan, pp)
    return plan, pp


PRIV_PJ = json.dumps({"name": "secretapp", "dependencies": {
    "@mycorp/secret-tool": "^1.2.0", "is-odd": "^3.0.0", "internal-billing": "2.0.0", "lodash": "4.17.21"}})
PRIV_LOCK = json.dumps({"lockfileVersion": 3, "packages": {
    "": {"name": "secretapp"},
    "node_modules/@mycorp/secret-tool": {
        "version": "1.2.5", "resolved": "https://npm.mycorp.internal/@mycorp/secret-tool/-/secret-tool-1.2.5.tgz"},
    "node_modules/is-odd": {"version": "3.0.1", "resolved": "https://registry.npmjs.org/is-odd/-/is-odd-3.0.1.tgz"},
    "node_modules/internal-billing": {
        "version": "2.0.0", "resolved": "https://user:pass@nexus.corp/repository/npm/internal-billing-2.0.0.tgz"},
    "node_modules/lodash": {"version": "4.17.21"}}})
SECRETS = ("SECRET123", "user:pass", "s3cr3tpw", "nexus.corp", "mycorp.internal", "pypi.mycorp", "tok@")


def coords_of(plan):
    return sorted(c for r in plan["requests"] for c in json.loads(r["body"]))


def reasons_of(plan):
    out = {}
    for e in plan["excluded"]:
        m = re.fullmatch(r"(\w+) \((\d+)\)", e["reason"])
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
    return out


def no_secrets(*blobs):
    text = "\n".join(b if isinstance(b, str) else json.dumps(b) for b in blobs)
    for s in SECRETS:
        assert s not in text, s


# ---- C1: private registries ---------------------------------------------------------

def test_private_registry_lock_resolved_and_npmrc(tmp_path):
    mk(tmp_path, {"package.json": PRIV_PJ, "package-lock.json": PRIV_LOCK,
                  ".npmrc": "@mycorp:registry=https://npm.mycorp.internal/\n"
                            "//npm.mycorp.internal/:_authToken=SECRET123\n"})
    plan = deps.make_plan(tmp_path)
    assert coords_of(plan) == ["npm/npmjs/-/is-odd/3.0.1", "npm/npmjs/-/lodash/4.17.21"]
    assert reasons_of(plan)["private_registry"] == 2
    assert "mycorp" not in json.dumps(plan) and "internal-billing" not in json.dumps(plan)
    no_secrets(plan, approval.render_preview(plan))


def test_private_registry_lock_host_alone(tmp_path):
    mk(tmp_path, {"package.json": PRIV_PJ, "package-lock.json": PRIV_LOCK})
    plan = deps.make_plan(tmp_path)
    assert coords_of(plan) == ["npm/npmjs/-/is-odd/3.0.1", "npm/npmjs/-/lodash/4.17.21"]
    assert reasons_of(plan)["private_registry"] == 2


def test_private_registry_lock_only_project(tmp_path):
    mk(tmp_path, {"package-lock.json": PRIV_LOCK})
    plan = deps.make_plan(tmp_path)
    assert coords_of(plan) == ["npm/npmjs/-/is-odd/3.0.1", "npm/npmjs/-/lodash/4.17.21"]
    assert reasons_of(plan)["private_registry"] == 2


def test_npmrc_scope_mapping_without_lock(tmp_path):
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {"@mycorp/x": "1.0.0", "is-odd": "3.0.1"}}),
                  ".npmrc": "# c\n@mycorp:registry=https://npm.mycorp.internal/\n"
                            "@pub:registry=https://registry.npmjs.org/\n_auth=SECRET123\n"})
    plan = deps.make_plan(tmp_path)
    assert coords_of(plan) == ["npm/npmjs/-/is-odd/3.0.1"]
    no_secrets(plan)


def test_npmrc_default_registry_private_excludes_everything(tmp_path):
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}}),
                  ".npmrc": "registry=https://user:pass@nexus.corp/npm/\n"})
    plan = deps.make_plan(tmp_path)
    assert plan["requests"] == [] and reasons_of(plan)["private_registry"] == 1
    no_secrets(plan, approval.render_preview(plan))


def test_npmrc_ancestor_applies_to_nested_manifest(tmp_path):
    mk(tmp_path, {".npmrc": "registry=https://nexus.corp/npm/\n",
                  "sub/package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}})})
    assert deps.make_plan(tmp_path)["requests"] == []


def test_npmrc_public_registry_changes_nothing(tmp_path):
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}}),
                  ".npmrc": "registry=https://registry.npmjs.org/\n"})
    assert coords_of(deps.make_plan(tmp_path)) == ["npm/npmjs/-/is-odd/3.0.1"]


def test_yarnrc_yml_scope_and_default(tmp_path):
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {"@mycorp/x": "1.0.0", "is-odd": "3.0.1"}}),
                  ".yarnrc.yml": 'nodeLinker: node-modules\nnpmScopes:\n  mycorp:\n'
                                 '    npmRegistryServer: "https://npm.mycorp.internal/"\n'
                                 '    npmAuthToken: SECRET123\n'})
    plan = deps.make_plan(tmp_path)
    assert coords_of(plan) == ["npm/npmjs/-/is-odd/3.0.1"]
    no_secrets(plan)
    mk(tmp_path / "d", {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}}),
                        ".yarnrc.yml": 'npmRegistryServer: "https://npm.corp/"\n'})
    assert deps.make_plan(tmp_path / "d")["requests"] == []


def test_codeload_tarball_is_local_or_git(tmp_path):
    lock = json.dumps({"lockfileVersion": 3, "packages": {
        "": {}, "node_modules/ghdep": {"version": "1.0.0", "resolved": "https://codeload.github.com/u/r/tar.gz/abc"}}})
    out = read(tmp_path, {"package.json": json.dumps({"dependencies": {"ghdep": "^1"}}), "package-lock.json": lock})
    assert [(d.name, d.excluded_reason) for d in out] == [("ghdep", "local_or_git")]


def test_lock_link_flag_is_local(tmp_path):
    lock = json.dumps({"lockfileVersion": 3, "packages": {
        "": {}, "node_modules/dep": {"version": "1.0.0", "link": True},
        "node_modules/dep2": {"version": "1.0.0", "resolved": "git+ssh://git@host/x.git#abc"}}})
    out = read(tmp_path, {"package.json": json.dumps({"dependencies": {"dep": "^1", "dep2": "^1"}}),
                          "package-lock.json": lock})
    assert {d.name: d.excluded_reason for d in out} == {"dep": "local_or_git", "dep2": "local_or_git"}


def test_lock_link_flag_alone_is_local(tmp_path):
    lock = json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/dep": {"version": "1.0.0", "link": True}}})
    out = read(tmp_path, {"package.json": json.dumps({"dependencies": {"dep": "1.0.0"}}), "package-lock.json": lock})
    assert [(d.name, d.excluded_reason) for d in out] == [("dep", "local_or_git")]


def test_lock_only_nested_entries_skipped(tmp_path):
    lock = json.dumps({"lockfileVersion": 3, "packages": {
        "": {}, "node_modules/a": {"version": "1.0.0"},
        "node_modules/a/node_modules/b": {"version": "2.0.0"}}})
    out = read(tmp_path, {"package-lock.json": lock})
    assert [(d.name, d.excluded_reason) for d in out] == [("a", None)]


REQ_PRIV = ("--index-url https://user:s3cr3tpw@pypi.mycorp.internal/simple\n"
            "mycorp-internal-sdk==3.4.5\nattrs==24.1.0\n")


def test_requirements_private_index_url(tmp_path):
    mk(tmp_path, {"requirements.txt": REQ_PRIV})
    plan = deps.make_plan(tmp_path)
    assert plan["requests"] == [] and reasons_of(plan)["private_registry"] == 2
    no_secrets(plan, approval.render_preview(plan))


def test_requirements_extra_index_is_possible(tmp_path):
    mk(tmp_path, {"requirements.txt": "--extra-index-url https://tok@pypi2.corp/simple\nattrs==24.1.0\n"})
    plan = deps.make_plan(tmp_path)
    assert plan["requests"] == [] and reasons_of(plan)["private_registry_possible"] == 1
    no_secrets(plan)
    pp = tmp_path / "plan.json"
    approval.write_plan(plan, pp)
    res = deps.run(pp, plan["sha256"], opener=lambda *a, **k: 1 / 0)
    assert res["excluded_by_reason"]["private_registry_possible"] == 1
    assert any("private_registry_possible" in n for n in res["notes"])
    no_secrets(res)


def test_requirements_public_index_url_still_sent(tmp_path):
    mk(tmp_path, {"requirements.txt": "-i https://pypi.org/simple\nattrs==24.1.0\n"})
    assert coords_of(deps.make_plan(tmp_path)) == ["pypi/pypi/-/attrs/24.1.0"]


def test_pip_conf_private_index(tmp_path):
    mk(tmp_path, {"requirements.txt": "attrs==24.1.0\n",
                  "pip.conf": "[global]\nindex-url = https://u:pw@pypi.corp/simple\n"})
    assert deps.make_plan(tmp_path)["requests"] == []


def test_poetry_source_key_and_project_sources(tmp_path):
    if sys.version_info < (3, 11):
        pytest.skip("tomllib needs Python 3.11+")
    out = read(tmp_path, {"pyproject.toml": '[tool.poetry.dependencies]\npython="^3.10"\n'
                          'pub="1.2.3"\nprivpkg={version="1.0.0", source="mycorp"}\n'})
    assert {d.name: d.excluded_reason for d in out} == {"pub": None, "privpkg": "private_registry"}
    out = read(tmp_path / "w", {"pyproject.toml": '[project]\ndependencies=["attrs==24.1.0"]\n'
                                '[[tool.poetry.source]]\nname="mycorp"\nurl="https://u:p@pypi.mycorp/simple"\n'})
    assert [(d.name, d.excluded_reason) for d in out] == [("attrs", "private_registry_possible")]
    out = read(tmp_path / "u", {"pyproject.toml": '[project]\ndependencies=["attrs==24.1.0", "other==1.0"]\n'
                                '[[tool.uv.index]]\nname="x"\nurl="https://pypi.corp/simple"\n'
                                '[tool.uv.sources]\nother={index="x"}\n'})
    assert {d.name: d.excluded_reason for d in out} == {"attrs": "private_registry_possible",
                                                         "other": "private_registry"}
    out = read(tmp_path / "p", {"pyproject.toml": '[project]\ndependencies=["attrs==24.1.0"]\n'
                                '[[tool.poetry.source]]\nname="pypi"\npriority="primary"\n'})
    assert [d.excluded_reason for d in out] == [None]


def test_scoped_packages_get_their_own_requests(tmp_path):
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {
        "@a/x": "1.0.0", "@a/y": "1.0.0", "@b/z": "1.0.0", "is-odd": "3.0.1", "lodash": "4.17.21"}})})
    plan = deps.make_plan(tmp_path)
    bodies = [json.loads(r["body"]) for r in plan["requests"]]
    assert bodies == [["npm/npmjs/-/is-odd/3.0.1", "npm/npmjs/-/lodash/4.17.21"],
                      ["npm/npmjs/@a/x/1.0.0", "npm/npmjs/@a/y/1.0.0"], ["npm/npmjs/@b/z/1.0.0"]]
    preview = approval.render_preview(plan)
    assert ("Scoped packages are sent by name. If a scope is your organisation's private scope, "
            "strike that request.") in preview
    assert "@a" in preview and "@b" in preview
    assert plan["cost"]["requests"] == 3
    struck = approval.strike(plan, {plan["requests"][1]["id"]})
    assert len(struck["requests"]) == 2


def test_many_scopes_are_bounded(tmp_path):
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {f"@s{i}/p": "1.0.0" for i in range(200)}})})
    plan = deps.make_plan(tmp_path)
    assert len(plan["requests"]) <= deps.MAX_SCOPE_REQUESTS + 4
    assert len(coords_of(plan)) == 200


def test_disclosure_says_public_service_and_private_names(tmp_path):
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}})})
    text = " ".join(deps.make_plan(tmp_path)["disclosure"]).lower()
    assert "public service" in text and "private" in text and "strike" in text


def test_quota_text_and_cost(tmp_path):
    assert "250 requests per 60 s" in deps.CD_QUOTA and "250;w=60" in deps.CD_QUOTA
    mk(tmp_path, {"requirements.txt": "\n".join(f"p{i}==1.0" for i in range(5000))})
    plan = deps.make_plan(tmp_path)
    assert plan["cost"]["requests"] == len(plan["requests"]) == 100
    assert deps.CD_QUOTA in plan["cost"]["quota"]


# ---- I1..I5 -------------------------------------------------------------------------

def test_pypi_local_version_not_sent(tmp_path):
    out = read(tmp_path, {"requirements.txt": "n==1.0+mycorp.secret\ntorch==2.1.0+cu118\nok==1.0\n"})
    assert {d.name: d.excluded_reason for d in out} == {"n": "local_version", "torch": "local_version", "ok": None}
    assert deps.coordinate(Dependency("pypi", "n", "1.0+x", "m", None)) is None


def test_too_many_counts_every_dropped_entry_and_manifest(tmp_path):
    mk(tmp_path, {"a/requirements.txt": "\n".join(f"p{i}==1.0" for i in range(20000)),
                  "b/package.json": json.dumps({"dependencies": {"zzz": "1.0.0"}}), "c/go.mod": "module x"})
    plan = _timed(lambda: deps.make_plan(tmp_path))
    ex = {(e["rel"], e["reason"]) for e in plan["excluded"]}
    assert ("a/requirements.txt", "too_many (15000)") in ex
    assert ("b/package.json", "too_many (1)") in ex
    assert ("c/go.mod", "unsupported_manifest (1)") in ex
    assert len(coords_of(plan)) == deps.MAX_DEPENDENCIES


def test_definition_status_other_and_noassertion():
    def d(declared):
        return {"licensed": {"declared": declared}, "scores": {"effective": 80}}
    got = deps.parse_definitions({"o": d("OTHER"), "n": d("NOASSERTION"), "e": d(""), "l": d("other"),
                                  "m": d("MIT")})
    assert got["o"]["status"] == "non_spdx" and got["o"]["declared"] == "OTHER"
    assert got["l"]["status"] == "non_spdx"
    assert got["n"]["status"] == "none_found" and got["n"]["declared"] is None
    assert got["e"]["status"] == "none_found"
    assert got["m"]["status"] == "declared"


def test_no_ownership_wording_for_dependencies(tmp_path):
    plan, pp = _plan_file(tmp_path, {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}})})
    body = {"npm/npmjs/-/is-odd/3.0.1": {"licensed": {"declared": "OTHER"}, "scores": {"effective": 50}}}
    res = deps.run(pp, plan["sha256"], opener=OkOpener(json.dumps(body).encode()))
    text = json.dumps(res).lower()
    assert "all rights reserved" not in text and "public domain" not in text
    assert res["dependencies"][0]["license"]["status"] == "non_spdx"
    assert res["dependencies"][0]["license"]["spdx"] is None


@pytest.mark.parametrize("name", ["package.json", "requirements.txt", "requirements-dev.txt"])
def test_dependency_named_like_a_manifest_does_not_kill_the_plan(tmp_path, name):
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {name: "1.0.0", "is-odd": "3.0.1"}}),
                  "requirements.txt": "attrs==24.1.0\n", "requirements-dev.txt": "flask==3.0.0\n"})
    plan = deps.make_plan(tmp_path)
    assert len(coords_of(plan)) == 3 and "npm/npmjs/-/is-odd/3.0.1" in coords_of(plan)
    assert reasons_of(plan)["bad_name"] == 1


def test_requirements_include_followed_inside_root(tmp_path):
    out = read(tmp_path, {"requirements.txt": "-r other.txt\n--requirement sub/more.txt\nattrs==24.1.0\n",
                          "other.txt": "flask==3.0.0\n-r requirements.txt\n",
                          "sub/more.txt": "-r ../other.txt\nsix==1.16.0\n"})
    assert {d.name for d in out if d.excluded_reason is None} == {"attrs", "flask", "six"}
    assert len([d for d in out if d.name == "flask"]) == 1          # cycle-safe, no double reading


def test_requirements_include_inherits_private_index(tmp_path):
    mk(tmp_path, {"requirements.txt": "--index-url https://pypi.corp/simple\n-r requirements-dev.txt\n",
                  "requirements-dev.txt": "pytest==8.0.0\n"})
    assert deps.make_plan(tmp_path)["requests"] == []


@pytest.mark.parametrize("line", ["-r ../outside.txt", "-r /etc/req.txt", "-r C:\\x\\y.txt",
                                  "-r https://x.test/r.txt", "-r ~/r.txt", "-c constraints.txt",
                                  "-r missing.txt", "-r $HOME/r.txt"])
def test_requirements_unfollowed_includes_are_listed(tmp_path, line):
    mk(tmp_path, {"requirements.txt": line + "\nattrs==24.1.0\n", "constraints.txt": "attrs==24.1.0\n"})
    plan = deps.make_plan(tmp_path)
    assert reasons_of(plan).get("unsupported_manifest") == 1
    assert coords_of(plan) == ["pypi/pypi/-/attrs/24.1.0"]
    approval.validate_plan(plan)


def test_requirements_include_depth_limited(tmp_path):
    files = {f"r{i}.txt": f"-r r{i + 1}.txt\np{i}==1.0\n" for i in range(9)}
    files["requirements.txt"] = "-r r0.txt\n"
    files["r9.txt"] = "deep==1.0\n"
    out = read(tmp_path, files)
    names = {d.name for d in out if d.excluded_reason is None}
    assert "p0" in names and "deep" not in names
    assert any(d.excluded_reason == "unsupported_manifest" for d in out)


@pytest.mark.parametrize("fname", ["setup.py", "setup.cfg", "environment.yml", "requirements.in", "Pipfile",
                                   "poetry.lock", "uv.lock", "dev.in"])
def test_more_unsupported_manifests_listed(tmp_path, fname):
    out = read(tmp_path, {fname: "x\n", "main.py": "1\n"})
    assert (fname, "unsupported_manifest") in [(d.manifest, d.excluded_reason) for d in out]


def test_non_string_pyproject_items_counted(tmp_path):
    if sys.version_info < (3, 11):
        pytest.skip("tomllib needs Python 3.11+")
    out = read(tmp_path, {"pyproject.toml": '[project]\ndependencies=["attrs==24.1.0", 1, {a=1}]\n'})
    assert sum(d.count for d in out if d.excluded_reason == "bad_entry") == 2


def test_non_dict_package_json_section_counted(tmp_path):
    out = read(tmp_path, {"package.json": json.dumps({"dependencies": ["a", 1]})})
    assert [d.excluded_reason for d in out] == ["bad_entry"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_gitignored_manifests_are_noted(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    mk(tmp_path, {".gitignore": "sub/\n", "sub/requirements.txt": "attrs==24.1.0\n", "main.py": "1\n"})
    plan = deps.make_plan(tmp_path)
    assert reasons_of(plan)["ignored_manifest"] == 1
    assert plan["requests"] == []


# ---- minor items ----------------------------------------------------------------------

def test_npm_alias_uses_lock_real_name(tmp_path):
    lock = json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/myalias": {
        "name": "real-pkg", "version": "1.0.0",
        "resolved": "https://registry.npmjs.org/real-pkg/-/real-pkg-1.0.0.tgz"}}})
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {"myalias": "npm:real-pkg@^1"}}),
                  "package-lock.json": lock})
    assert coords_of(deps.make_plan(tmp_path)) == ["npm/npmjs/-/real-pkg/1.0.0"]
    out = read(tmp_path / "n", {"package.json": json.dumps({"dependencies": {"myalias": "npm:real-pkg@^1"}})})
    assert [(d.name, d.excluded_reason) for d in out] == [("myalias", "alias")]


def test_confidence_clamped_and_non_finite_rejected():
    def d(eff):
        return {"licensed": {"declared": "MIT"}, "scores": {"effective": eff}}
    nan = json.loads('{"a": {"licensed": {"declared": "MIT"}, "scores": {"effective": NaN}},'
                     ' "b": {"licensed": {"declared": "MIT"}, "scores": {"effective": Infinity}}}')
    got = deps.parse_definitions(nan)
    for k in "ab":
        assert got[k]["status"] == "unknown" and got[k]["confidence"] is None and got[k]["known"] is False
    got = deps.parse_definitions({"big": d(1e308), "neg": d(-5), "str": d("97"), "t": d(True), "ok": d(87)})
    assert got["big"]["confidence"] == 1.0 and got["big"]["known"] is True
    assert got["neg"]["confidence"] == 0.0 and got["neg"]["known"] is False
    assert got["str"]["known"] is False and got["t"]["known"] is False
    assert got["ok"]["confidence"] == 0.87
    json.dumps(got, allow_nan=False)


def test_results_file_is_strict_json(tmp_path):
    plan, pp = _plan_file(tmp_path, {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}})})
    body = b'{"npm/npmjs/-/is-odd/3.0.1": {"licensed": {"declared": "MIT"}, "scores": {"effective": NaN}}}'
    res = deps.run(pp, plan["sha256"], opener=OkOpener(body))
    json.dumps(res, allow_nan=False)
    out = tmp_path / "r.json"
    deps.main(["run", str(pp), "--sha256", plan["sha256"], "--out", str(out)])     # blocked network
    json.loads(out.read_text(encoding="utf-8"), parse_constant=lambda c: 1 / 0)


def test_declared_discovered_conflict_and_truncation():
    d = {"licensed": {"declared": "MIT", "facets": {"core": {"discovered": {"expressions": ["GPL-3.0-only"]}}}},
         "scores": {"effective": 90}}
    same = {"licensed": {"declared": "MIT", "facets": {"core": {"discovered": {"expressions": ["MIT"]}}}},
            "scores": {"effective": 90}}
    long = {"licensed": {"declared": "X" * 1000}, "scores": {"effective": 90}}
    got = deps.parse_definitions({"c": d, "s": same, "l": long})
    assert got["c"]["conflict"] is True and got["c"]["declared"] == "MIT" and got["c"]["discovered"] == ["GPL-3.0-only"]
    assert got["s"]["conflict"] is False and got["s"]["truncated"] is False
    assert got["l"]["truncated"] is True and len(got["l"]["declared"]) == deps.MAX_TEXT


def test_no_manifests_note(tmp_path, capsys):
    mk(tmp_path / "proj", {"main.py": "print(1)\n"})
    out = tmp_path / "p.json"
    assert deps.main(["plan", str(tmp_path / "proj"), "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert "no_manifests" in printed["preview"]
    res = deps.run(out, printed["sha256"], opener=lambda *a, **k: 1 / 0)
    assert any(n.startswith("no_manifests") for n in res["notes"])
    assert deps.main(["run", str(out), "--sha256", printed["sha256"], "--out", str(tmp_path / "r.json")]) == 0
    assert "no_manifests" in capsys.readouterr().out


def test_plan_refuses_to_overwrite(tmp_path, capsys):
    mk(tmp_path / "proj", {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}})})
    out = tmp_path / "p.json"
    assert deps.main(["plan", str(tmp_path / "proj"), "--out", str(out)]) == 0
    capsys.readouterr()
    before = out.read_bytes()
    assert deps.main(["plan", str(tmp_path / "proj"), "--out", str(out)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "output_exists"
    assert out.read_bytes() == before


def test_requirements_hash_on_same_line_still_pinned(tmp_path):
    out = read(tmp_path, {"requirements.txt": "attrs==24.1.0 --hash=sha256:abcdef --hash=sha256:012345\n"
                                              "six==1.16.0 \\\n    --hash=sha256:aa\n"})
    assert {d.name: (d.version, d.excluded_reason) for d in out} == {
        "attrs": ("24.1.0", None), "six": ("1.16.0", None)}


# ---- mutation survivors -----------------------------------------------------------------

def test_run_incomplete_response_is_error(tmp_path):
    plan, pp = _plan_file(tmp_path, {"requirements.txt": "a==1.0\nb==2.0\n"})
    body = {"pypi/pypi/-/a/1.0": {"licensed": {"declared": "MIT"}, "scores": {"effective": 50}}}
    res = deps.run(pp, plan["sha256"], opener=OkOpener(json.dumps(body).encode()))
    assert [e["code"] for e in res["errors"]] == ["incomplete_response"]


@pytest.mark.parametrize("name", ['a","npm/npmjs/-/evil/1', "a b", "../x", "naïve", "x" * 300, "@sc/x/y", "a\nb"])
def test_coordinate_rejects_hostile_names_directly(name):
    assert deps.coordinate(Dependency("npm", name, "1.0.0", "m", None)) is None
    assert deps.coordinate(Dependency("pypi", name, "1.0.0", "m", None)) is None


def test_inventory_require_sources_flag(tmp_path):
    mk(tmp_path, {"package.json": "{}", "node_modules/x/index.js": "1"})
    with pytest.raises(InventoryError) as e:
        inventory(tmp_path)
    assert e.value.code == "no_source_files"
    assert inventory(tmp_path, require_sources=False).sources == []
