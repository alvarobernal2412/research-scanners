"""Strict tier, fix round 2: workspaces, continuations, more config files, fail-closed limits."""
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import approval  # noqa: E402
import deps  # noqa: E402
from deps import Dependency  # noqa: E402
from inventory import inventory  # noqa: E402

NL = "\n"
BS = "\\"


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


def coords_of(plan):
    return sorted(c for r in plan["requests"] for c in json.loads(r["body"]))


def reasons_of(plan):
    out = {}
    for e in plan["excluded"]:
        m = re.fullmatch(r"(\w+) \((\d+)\)", e["reason"])
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
    return out


def sent(tmp_path, files):
    mk(tmp_path, files)
    return coords_of(deps.make_plan(tmp_path))


def timed(fn, budget=2.0):
    t = time.perf_counter()
    r = fn()
    dt = time.perf_counter() - t
    assert dt < budget, f"{dt:.2f}s"
    return r


def PJ(**d):
    return json.dumps({"dependencies": d})


ISODD = PJ(**{"is-odd": "3.0.1"})
ODD_COORD = ["npm/npmjs/-/is-odd/3.0.1"]
ATTRS = ["pypi/pypi/-/attrs/24.1.0"]


# ---- N1: npm workspaces -----------------------------------------------------------------

WS_LOCK = json.dumps({"lockfileVersion": 3, "packages": {
    "": {"workspaces": ["packages/*"]},
    "node_modules/internal-billing": {
        "version": "2.0.0", "resolved": "https://npm.mycorp.internal/internal-billing/-/internal-billing-2.0.0.tgz"},
    "node_modules/internal-ui": {"resolved": "packages/ui", "link": True},
    "node_modules/is-odd": {"version": "3.0.1", "resolved": "https://registry.npmjs.org/is-odd/-/is-odd-3.0.1.tgz"}}})


def test_workspace_member_uses_root_lock(tmp_path):
    got = sent(tmp_path, {
        "package.json": json.dumps({"workspaces": ["packages/*"]}), "package-lock.json": WS_LOCK,
        "packages/web/package.json": PJ(**{"internal-billing": "2.0.0", "internal-ui": "1.0.0", "is-odd": "3.0.1"})})
    assert got == ODD_COORD
    plan = deps.make_plan(tmp_path)
    assert reasons_of(plan)["private_registry"] == 1 and reasons_of(plan)["local_or_git"] == 1


def test_workspace_member_lock_without_entry_but_root_lock_private(tmp_path):
    member_lock = json.dumps({"lockfileVersion": 3, "packages": {
        "": {}, "node_modules/is-odd": {"version": "3.0.1", "resolved": "https://registry.npmjs.org/is-odd/-/i.tgz"}}})
    got = sent(tmp_path, {
        "package.json": json.dumps({"workspaces": ["packages/*"]}), "package-lock.json": WS_LOCK,
        "packages/web/package.json": PJ(**{"internal-billing": "2.0.0", "is-odd": "3.0.1"}),
        "packages/web/package-lock.json": member_lock})
    assert got == ODD_COORD


def test_private_entry_in_any_applicable_lock_wins(tmp_path):
    member_lock = json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/internal-billing": {
        "version": "2.0.0", "resolved": "https://registry.npmjs.org/internal-billing/-/i.tgz"}}})
    got = sent(tmp_path, {
        "package.json": json.dumps({"workspaces": ["packages/*"]}), "package-lock.json": WS_LOCK,
        "packages/web/package.json": PJ(**{"internal-billing": "2.0.0"}),
        "packages/web/package-lock.json": member_lock})
    assert got == []


def test_hoisted_nested_entry_with_private_host(tmp_path):
    lock = json.dumps({"lockfileVersion": 3, "packages": {
        "": {}, "node_modules/a": {"version": "1.0.0", "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz"},
        "node_modules/a/node_modules/b": {"version": "1.0.0", "resolved": "https://npm.mycorp.internal/b.tgz"},
        "packages/web/node_modules/c": {"version": "1.0.0", "resolved": "https://nexus.corp/c.tgz"}}})
    assert sent(tmp_path, {"package.json": PJ(a="1.0.0", b="1.0.0", c="1.0.0"), "package-lock.json": lock}) == \
        ["npm/npmjs/-/a/1.0.0"]


def test_workspace_package_referenced_by_name(tmp_path):
    got = sent(tmp_path, {
        "package.json": json.dumps({"workspaces": ["packages/*"], "dependencies": {
            "@mycorp/ui": "workspace:*", "@mycorp/core": "1.0.0", "is-odd": "3.0.1"}}),
        "packages/ui/package.json": json.dumps({"name": "@mycorp/ui", "version": "1.0.0"}),
        "packages/core/package.json": json.dumps({"name": "@mycorp/core", "version": "1.0.0"})})
    assert got == ODD_COORD
    plan = deps.make_plan(tmp_path)
    assert reasons_of(plan)["local_or_git"] == 2


def test_no_lock_and_no_signal_still_sent(tmp_path):
    assert sent(tmp_path, {"package.json": ISODD, "packages/x/package.json": PJ(lodash="4.17.21")}) == \
        ["npm/npmjs/-/is-odd/3.0.1", "npm/npmjs/-/lodash/4.17.21"]


def test_ancestor_lock_gives_version_for_range(tmp_path):
    lock = json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/is-odd": {
        "version": "3.0.1", "resolved": "https://registry.npmjs.org/is-odd/-/is-odd-3.0.1.tgz"}}})
    assert sent(tmp_path, {"package.json": "{}", "package-lock.json": lock,
                           "packages/web/package.json": PJ(**{"is-odd": "^3.0.0"})}) == ODD_COORD


def test_npm_shrinkwrap_is_consulted(tmp_path):
    lock = json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/internal-billing": {
        "version": "2.0.0", "resolved": "https://npm.mycorp.internal/x.tgz"}}})
    assert sent(tmp_path, {"package.json": PJ(**{"internal-billing": "2.0.0"}), "npm-shrinkwrap.json": lock}) == []


# ---- N2: continuations --------------------------------------------------------------------

def test_continued_index_url_is_joined(tmp_path):
    assert sent(tmp_path, {"requirements.txt": f"--index-url {BS}{NL}  https://pypi.mycorp.internal/simple{NL}"
                                               f"attrs==24.1.0{NL}"}) == []
    plan = deps.make_plan(tmp_path)
    assert reasons_of(plan)["private_registry"] == 1


def test_continued_extra_index_url_is_joined(tmp_path):
    assert sent(tmp_path, {"requirements.txt": f"--extra-index-url {BS}{NL}  https://pypi.mycorp.internal/simple{NL}"
                                               f"attrs==24.1.0{NL}"}) == []
    assert reasons_of(deps.make_plan(tmp_path))["private_registry_possible"] == 1


def test_continued_pin_is_joined_lf_and_crlf(tmp_path):
    assert sent(tmp_path, {"requirements.txt": f"attrs=={BS}{NL}24.1.0{NL}"}) == ATTRS
    assert sent(tmp_path / "w", {"requirements.txt": f"attrs=={BS}\r\n24.1.0\r\n"}) == ATTRS


def test_hash_continuation_still_pinned(tmp_path):
    out = read(tmp_path, {"requirements.txt": f"attrs==24.1.0 {BS}{NL}    --hash=sha256:abc {BS}{NL}"
                                              f"    --hash=sha256:def{NL}six==1.16.0{NL}"})
    assert {d.name: d.excluded_reason for d in out} == {"attrs": None, "six": None}


def test_overlong_continuation_fails_closed(tmp_path):
    text = ("--extra-index-url " + BS + NL) + (("a" * 1000 + BS + NL) * 100) + "end" + NL + "attrs==24.1.0" + NL
    assert sent(tmp_path, {"requirements.txt": text}) == []


def test_comment_after_joined_line(tmp_path):
    assert sent(tmp_path, {"requirements.txt": f"attrs== {BS}{NL}24.1.0 # note{NL}"}) == ATTRS


# ---- N3: option spellings -----------------------------------------------------------------

@pytest.mark.parametrize("line,reason", [
    ("-ihttps://pypi.mycorp.internal/simple", "private_registry"),
    ("-i https://pypi.mycorp.internal/simple", "private_registry"),
    ("--index-url=https://pypi.mycorp.internal/simple", "private_registry"),
    ("--index-url https://pypi.mycorp.internal/simple", "private_registry"),
    ("--extra-index-url=https://pypi.mycorp.internal/simple", "private_registry_possible"),
    ("-fhttps://mycorp.internal/wheels", "private_registry_possible"),
    ("-f ./wheels", "private_registry_possible"),
    ("--find-links=https://mycorp.internal/wheels", "private_registry_possible"),
    ("--find-links https://mycorp.internal/wheels", "private_registry_possible"),
    ("--trusted-host pypi.mycorp.internal", "private_registry_possible"),
    ("--trusted-host=pypi.mycorp.internal:8080", "private_registry_possible"),
])
def test_requirements_option_spellings(tmp_path, line, reason):
    assert sent(tmp_path, {"requirements.txt": line + NL + "attrs==24.1.0" + NL}) == []
    assert reasons_of(deps.make_plan(tmp_path)) == {reason: 1}


@pytest.mark.parametrize("line", ["--trusted-host pypi.org", "-i https://pypi.org/simple",
                                  "--index-url=https://pypi.org/simple/", "--no-index-ignored-option"])
def test_requirements_public_options_change_nothing(tmp_path, line):
    assert sent(tmp_path, {"requirements.txt": line + NL + "attrs==24.1.0" + NL}) == ATTRS


# ---- N4: more configuration files ---------------------------------------------------------

def test_uv_toml(tmp_path):
    if sys.version_info < (3, 11):
        pytest.skip("tomllib needs Python 3.11+")
    base = {"requirements.txt": "attrs==24.1.0" + NL}
    assert sent(tmp_path / "a", {**base, "uv.toml": 'index-url = "https://u:SECRET@pypi.mycorp.internal/simple"\n'}) == []
    assert reasons_of(deps.make_plan(tmp_path / "a")) == {"private_registry": 1}
    assert sent(tmp_path / "b", {**base, "uv.toml": 'extra-index-url = ["https://pypi.mycorp.internal/simple"]\n'}) == []
    assert reasons_of(deps.make_plan(tmp_path / "b")) == {"private_registry_possible": 1}
    assert sent(tmp_path / "c", {**base, "uv.toml": '[[index]]\nname="x"\nurl="https://pypi.mycorp.internal/simple"\n'}) == []
    assert sent(tmp_path / "d", {**base, "uv.toml": '[pip]\nindex-url="https://pypi.mycorp.internal/simple"\n'}) == []
    assert sent(tmp_path / "e", {**base, "uv.toml": 'find-links = ["./wheels"]\n'}) == []
    assert sent(tmp_path / "f", {**base, "uv.toml": 'index-url = "https://pypi.org/simple"\n'}) == ATTRS
    assert "SECRET" not in json.dumps(deps.make_plan(tmp_path / "a"))


def test_pyproject_uv_pip_table_and_find_links(tmp_path):
    if sys.version_info < (3, 11):
        pytest.skip("tomllib needs Python 3.11+")
    head = '[project]\ndependencies=["attrs==24.1.0"]\n'
    assert sent(tmp_path / "a", {"pyproject.toml": head + '[tool.uv.pip]\nindex-url="https://pypi.mycorp.internal/simple"\n'}) == []
    assert sent(tmp_path / "b", {"pyproject.toml": head + '[tool.uv]\nfind-links=["./wheels"]\n'}) == []
    assert sent(tmp_path / "c", {"pyproject.toml": head + '[tool.uv.pip]\nfind-links=["./wheels"]\n'}) == []
    assert sent(tmp_path / "d", {"pyproject.toml": head}) == ATTRS


def test_poetry_toml_repositories(tmp_path):
    if sys.version_info < (3, 11):
        pytest.skip("tomllib needs Python 3.11+")
    files = {"pyproject.toml": '[project]\ndependencies=["attrs==24.1.0"]\n',
             "poetry.toml": '[repositories.corp]\nurl="https://u:SECRET@pypi.mycorp.internal/simple"\n'}
    assert sent(tmp_path, files) == []
    assert reasons_of(deps.make_plan(tmp_path)) == {"private_registry_possible": 1}
    assert "SECRET" not in json.dumps(deps.make_plan(tmp_path))


def test_yarn_classic_yarnrc(tmp_path):
    pj = PJ(**{"is-odd": "3.0.1", "@corp/x": "1.0.0"})
    assert sent(tmp_path / "a", {"package.json": pj, ".yarnrc": 'registry "https://npm.mycorp.internal/"\n'}) == []
    assert sent(tmp_path / "b", {"package.json": pj, ".yarnrc": '"@corp:registry" "https://npm.mycorp.internal/"\n'}) == \
        ODD_COORD
    assert sent(tmp_path / "c", {"package.json": pj, ".yarnrc": 'registry https://registry.yarnpkg.com\n'}) == \
        ["npm/npmjs/-/is-odd/3.0.1", "npm/npmjs/@corp/x/1.0.0"]
    assert sent(tmp_path / "d", {"package.json": pj, ".yarnrc": 'registry "https://npm.mycorp.internal/"\n'
                                                              'npmAuthToken "SECRET123"\n'}) == []
    assert "SECRET123" not in json.dumps(deps.make_plan(tmp_path / "d"))


# ---- N5: fail closed on config limits -----------------------------------------------------

def test_oversize_npmrc_fails_closed_and_is_noted(tmp_path):
    big = "#" + "x" * 300_000 + NL + "registry=https://npm.mycorp.internal/" + NL
    assert sent(tmp_path, {"package.json": ISODD, ".npmrc": big}) == []
    plan = deps.make_plan(tmp_path)
    assert reasons_of(plan) == {"private_registry_possible": 1, "config_unreadable": 1}
    assert {"rel": ".npmrc", "reason": "config_unreadable (1)"} in plan["excluded"]


def test_long_npmrc_registry_after_line_2000(tmp_path):
    text = NL * 2500 + "registry=https://npm.mycorp.internal/" + NL
    assert sent(tmp_path, {"package.json": ISODD, ".npmrc": text}) == []
    assert reasons_of(deps.make_plan(tmp_path)) == {"private_registry": 1}


def test_binary_and_non_utf8_npmrc_fail_closed(tmp_path):
    assert sent(tmp_path / "a", {"package.json": ISODD, ".npmrc": b"\x00\x01\x02binary\xff"}) == []
    assert sent(tmp_path / "b", {"package.json": ISODD, ".npmrc": b"registry=\xff\xfe\x80broken"}) == []
    assert "config_unreadable" in reasons_of(deps.make_plan(tmp_path / "a"))


def test_npmrc_directory_is_absent_but_noted(tmp_path):
    mk(tmp_path, {"package.json": ISODD})
    (tmp_path / ".npmrc").mkdir()
    plan = deps.make_plan(tmp_path)
    assert coords_of(plan) == ODD_COORD
    assert reasons_of(plan) == {"config_unreadable": 1}


def test_oversize_pip_conf_and_bad_uv_toml_fail_closed(tmp_path):
    big = "[global]\n" + "# " + "x" * 300_000 + NL + "index-url = https://pypi.mycorp.internal/simple" + NL
    assert sent(tmp_path / "a", {"requirements.txt": "attrs==24.1.0" + NL, "pip.conf": big}) == []
    assert "config_unreadable" in reasons_of(deps.make_plan(tmp_path / "a"))
    assert sent(tmp_path / "b", {"requirements.txt": "attrs==24.1.0" + NL, "uv.toml": "[[[ not toml"}) == []
    assert "config_unreadable" in reasons_of(deps.make_plan(tmp_path / "b"))


def test_unreadable_config_note_in_results(tmp_path):
    mk(tmp_path, {"package.json": ISODD, ".npmrc": b"\x00\x00"})
    plan = deps.make_plan(tmp_path)
    pp = tmp_path / "p.json"
    approval.write_plan(plan, pp)
    res = deps.run(pp, plan["sha256"], opener=lambda *a, **k: 1 / 0)
    assert any(n.startswith("config_unreadable") for n in res["notes"])
    assert res["excluded_by_reason"]["config_unreadable"] == 1


# ---- mutation survivors: look-alike hosts ---------------------------------------------------

LOOKALIKES = ["registry.npmjs.org.evil.com", "registry-npmjs.org", "xregistry.npmjs.org", "evil.com",
              "registry.npmjs.org@evil.com", "registry.npmjs.org.", "npmjs.org", "registry.npmjs.org:8443",
              "registry.npmmirror.com", "evil.com#@registry.npmjs.org", "registry.npmjs.org" + BS + "@evil.com"]


@pytest.mark.parametrize("host", LOOKALIKES)
def test_lookalike_hosts_in_lock_resolved(tmp_path, host):
    lock = json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/is-odd": {
        "version": "3.0.1", "resolved": f"https://{host}/is-odd/-/is-odd-3.0.1.tgz"}}})
    assert sent(tmp_path, {"package.json": ISODD, "package-lock.json": lock}) == []


@pytest.mark.parametrize("host", LOOKALIKES)
def test_lookalike_hosts_in_npmrc_and_yarnrc(tmp_path, host):
    assert sent(tmp_path / "a", {"package.json": ISODD, ".npmrc": f"registry=https://{host}/\n"}) == []
    assert sent(tmp_path / "b", {"package.json": ISODD, ".yarnrc.yml": f'npmRegistryServer: "https://{host}/"\n'}) == []
    assert sent(tmp_path / "c", {"package.json": ISODD, ".yarnrc": f'registry "https://{host}/"\n'}) == []


@pytest.mark.parametrize("host", ["pypi.org.evil.com", "pypi-org.com", "xpypi.org", "pypi.org@evil.com",
                                  "pypi.org.", "test.pypi.org", "pypi.org:8443", "evil.com"])
def test_lookalike_hosts_for_pypi_index(tmp_path, host):
    assert sent(tmp_path, {"requirements.txt": f"-i https://{host}/simple{NL}attrs==24.1.0{NL}"}) == []


def test_public_hosts_still_public(tmp_path):
    lock = json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/is-odd": {
        "version": "3.0.1", "resolved": "https://registry.npmjs.org/is-odd/-/is-odd-3.0.1.tgz"}}})
    assert sent(tmp_path, {"package.json": ISODD, "package-lock.json": lock}) == ODD_COORD
    assert deps._public("https://registry.npmjs.org:443/x", deps.PUBLIC_NPM_HOSTS)
    assert not deps._public("ftp://registry.npmjs.org/x", deps.PUBLIC_NPM_HOSTS)


# ---- mutation survivors: includes and root checks --------------------------------------------

def test_vendored_include_not_followed(tmp_path):
    out = read(tmp_path, {"requirements.txt": "-r node_modules/x/requirements.txt" + NL + "attrs==24.1.0" + NL,
                          "node_modules/x/requirements.txt": "evil==1.0" + NL})
    assert "evil" not in {d.name for d in out}
    assert any(d.excluded_reason == "unsupported_manifest" for d in out)


def test_traversal_include_not_followed(tmp_path):
    out = read(tmp_path / "proj", {"requirements.txt": "-r ../secret.txt" + NL + "-r a/../../secret.txt" + NL})
    (tmp_path / "secret.txt").write_text("leak==1.0\n")
    out = deps.read_dependencies(inventory(tmp_path / "proj", require_sources=False))
    assert "leak" not in {d.name for d in out}


def test_inside_root_helper(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    assert deps._inside_root(root, root / "sub")
    assert deps._inside_root(root, root / "sub" / ".." / "sub" / "x.txt")
    assert not deps._inside_root(root, tmp_path / "other")
    assert not deps._inside_root(root, root / ".." / "other")
    assert not deps._inside_root(root, root / "sub" / ".." / ".." / "other")
    link = root / "sub" / "out"
    try:
        os.symlink(tmp_path / "other", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert not deps._inside_root(root, link / "x.txt")


def _dir_link(link, target):
    """A directory symlink, or on Windows a junction (needs no privilege); skip if neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if r.returncode == 0:
            return
    pytest.skip("directory links not available")


def test_include_through_directory_link_outside_root_not_followed(tmp_path):
    proj = tmp_path / "proj"
    mk(proj, {"requirements.txt": "-r jump/outside.txt" + NL + "attrs==24.1.0" + NL})
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "outside.txt").write_text("leak==1.0" + NL)
    _dir_link(proj / "jump", tmp_path / "other")
    out = deps.read_dependencies(inventory(proj, require_sources=False))
    assert "leak" not in {d.name for d in out}
    assert "attrs" in {d.name for d in out}


def test_unreadable_lockfile_fails_closed(tmp_path):
    assert sent(tmp_path, {"package.json": ISODD, "package-lock.json": "{ not json"}) == []
    assert reasons_of(deps.make_plan(tmp_path)) == {"private_registry_possible": 1, "config_unreadable": 1}


def test_symlinked_include_outside_root_not_followed(tmp_path):
    proj = tmp_path / "proj"
    mk(proj, {"requirements.txt": "-r link.txt" + NL})
    (tmp_path / "outside.txt").write_text("leak==1.0\n")
    try:
        os.symlink(tmp_path / "outside.txt", proj / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    out = deps.read_dependencies(inventory(proj, require_sources=False))
    assert "leak" not in {d.name for d in out}


def test_find_links_marks_file_possible(tmp_path):
    assert sent(tmp_path, {"requirements.txt": "--find-links https://mycorp.internal/wheels" + NL +
                                               "attrs==24.1.0" + NL}) == []
    assert reasons_of(deps.make_plan(tmp_path)) == {"private_registry_possible": 1}


# ---- minor ----------------------------------------------------------------------------------

def test_private_registry_note_mentions_mirrors():
    assert "mirror" in deps._REASON_NOTES["private_registry"].lower()


def test_scoped_purpose_carries_warning_and_pluralisation(tmp_path):
    mk(tmp_path, {"package.json": PJ(**{"@a/x": "1.0.0", "@b/y": "1.0.0", "@b/z": "1.0.0", "is-odd": "3.0.1"})})
    plan = deps.make_plan(tmp_path)
    purposes = [r["purpose"] for r in plan["requests"]]
    assert "1 unscoped dependency" in purposes[0] and "1 dependencies" not in purposes[0]
    assert "1 package of scope @a" in purposes[1] and "2 packages of scope @b" in purposes[2]
    for p in purposes[1:]:
        assert "Scoped packages are sent by name. If this scope is your organisation's private scope, " \
               "strike this request." in p
    preview = approval.render_preview(plan)
    assert preview.index("Scoped packages are sent by name") < preview.index('["npm/npmjs/@a/x/1.0.0"]')


def test_sanitised_names_for_local_specs(tmp_path):
    out = read(tmp_path, {"requirements.txt":
                          "pkg @ https://user:SECRETUSER@host.corp/x.whl" + NL +
                          "-e git+https://tok:SECRETTOK@h.corp/x.git#egg=x" + NL +
                          "./local/SECRETPATH" + NL +
                          "git+https://u:SECRETVCS@h/x.git" + NL +
                          "bad line https://u:SECRETBAD@h/x" + NL,
                          "package.json": json.dumps({"dependencies": {"git+https://u:SECRETKEY@h/x.git": "1.0.0"}})})
    assert out
    blob = repr(out) + str(out) + json.dumps([d.__dict__ for d in out])
    assert "SECRET" not in blob
    names = {d.name for d in out}
    assert {"<url-spec>", "<vcs-spec>", "<path-spec>"} <= names
    plan = deps.make_plan(tmp_path)
    assert "SECRET" not in json.dumps(plan) + approval.render_preview(plan)


# ---- M5: performance and bounds ---------------------------------------------------------------

def test_perf_300_package_json_depth_30(tmp_path):
    files = {("/".join(f"d{j}" for j in range(30)) + f"/p{i}/package.json"): PJ(a="1.0.0") for i in range(300)}
    files[".npmrc"] = "registry=https://registry.npmjs.org/" + NL
    mk(tmp_path, files)
    inv = inventory(tmp_path, require_sources=False)
    found = timed(lambda: deps.read_dependencies(inv))
    assert len([d for d in found if d.excluded_reason is None]) == 300


def test_perf_3000_package_json_depth_1(tmp_path):
    mk(tmp_path, {f"p{i}/package.json": PJ(**{f"a{i}": "1.0.0"}) for i in range(3000)})
    inv = inventory(tmp_path, require_sources=False)          # the inventory walk is timed on its own
    # reading 3000 small files costs ~1.4 s of raw I/O on the dev machine, so the budget is 3 s here
    found = timed(lambda: deps.read_dependencies(inv), 3.0)
    assert len([d for d in found if d.excluded_reason is None]) == 3000


def test_perf_5000_distinct_includes_capped(tmp_path):
    files = {"requirements.txt": NL.join(f"-r f{i}.txt" for i in range(5000)) + NL}
    files.update({f"f{i}.txt": f"p{i}==1.0" + NL for i in range(5000)})
    mk(tmp_path, files)
    inv = inventory(tmp_path, require_sources=False)
    found = timed(lambda: deps.read_dependencies(inv), 3.0)
    assert len([d for d in found if d.excluded_reason is None]) <= deps.MAX_INCLUDES_FOLLOWED
    assert any(d.manifest == "(include limit)" for d in found)


def test_perf_5000_missing_includes(tmp_path):
    mk(tmp_path, {"requirements.txt": NL.join(f"-r m{i}.txt" for i in range(5000)) + NL})
    plan = timed(lambda: deps.make_plan(tmp_path), 3.0)
    assert len(plan["excluded"]) <= deps.MAX_UNSUPPORTED_LABELS + 2


def test_text_cache_is_bounded(tmp_path):
    mk(tmp_path, {f"r{i}.txt": "a==1.0" + NL for i in range(400)})
    ctx = deps._Ctx(inventory(tmp_path, require_sources=False))
    for i in range(400):
        assert ctx.text(f"r{i}.txt") is not None
    assert len(ctx._bytes) <= deps.TEXT_CACHE_MAX


def test_perf_self_include_spellings_and_fanout(tmp_path):
    spell = NL.join("-r " + "./" * i + "requirements.txt" for i in range(1000)) + NL + "attrs==24.1.0" + NL
    mk(tmp_path, {"requirements.txt": spell})
    assert coords_of(timed(lambda: deps.make_plan(tmp_path))) == ATTRS


def test_perf_big_config_files(tmp_path):
    mk(tmp_path, {"package.json": ISODD, ".npmrc": ("#x" + NL) * 100_000,
                  ".yarnrc.yml": "npmScopes:" + NL + ("  a:" + NL) * 60_000})
    timed(lambda: deps.make_plan(tmp_path))
    mk(tmp_path / "w", {"package.json": ISODD, ".npmrc": "@" + "a" * 250_000 + ":regist" + NL})
    timed(lambda: deps.make_plan(tmp_path / "w"))


def test_perf_long_option_line(tmp_path):
    mk(tmp_path, {"requirements.txt": "--index-url " + "a" * 15_000_000 + NL})
    timed(lambda: deps.make_plan(tmp_path))
