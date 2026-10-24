"""Strict tier: declared dependencies and ClearlyDefined licences."""
import io
import json
import re
import sys
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"))

import approval  # noqa: E402
import deps  # noqa: E402
from deps import Dependency  # noqa: E402
from inventory import inventory  # noqa: E402
from replay import ReplayOpener  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://api.clearlydefined.io/definitions"


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


def by_name(items):
    return {d.name: d for d in items}


def test_package_lock_versions_preferred(tmp_path):
    out = read(tmp_path, {
        "package.json": json.dumps({"dependencies": {"is-odd": "^3.0.0", "@sc/pkg": "~1.0.0"}}),
        "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {
            "": {"name": "x"},
            "node_modules/is-odd": {"version": "3.0.1"},
            "node_modules/@sc/pkg": {"version": "1.0.4"},
            "node_modules/is-odd/node_modules/is-number": {"version": "6.0.0"}}}),
    })
    d = by_name(out)
    assert (d["is-odd"].version, d["is-odd"].excluded_reason) == ("3.0.1", None)
    assert (d["@sc/pkg"].version, d["@sc/pkg"].excluded_reason) == ("1.0.4", None)
    assert "is-number" not in d          # transitive: not a declared dependency


def test_package_lock_v1_format(tmp_path):
    out = read(tmp_path, {
        "package.json": json.dumps({"dependencies": {"left-pad": "^1.0.0"}}),
        "package-lock.json": json.dumps({"lockfileVersion": 1, "dependencies": {"left-pad": {"version": "1.3.0"}}}),
    })
    assert by_name(out)["left-pad"].version == "1.3.0"


def test_package_json_range_without_lock_is_unpinned(tmp_path):
    out = read(tmp_path, {"package.json": json.dumps(
        {"dependencies": {"a-range": "^1.2.3", "exact-one": "1.2.3", "tagged": "latest"}})})
    d = by_name(out)
    assert d["a-range"].excluded_reason == "unpinned" and d["a-range"].version is None
    assert d["tagged"].excluded_reason == "unpinned"
    assert d["exact-one"].version == "1.2.3" and d["exact-one"].excluded_reason is None
    assert deps.coordinate(d["a-range"]) is None


def test_requirements_pinned_only(tmp_path):
    out = read(tmp_path, {"requirements.txt":
                          "a==1.0\nb>=2\n# c==3\nd==2.0  # note\nE_f==1.1 ; python_version<'4'\n"})
    d = by_name(out)
    assert (d["a"].version, d["a"].excluded_reason) == ("1.0", None)
    assert d["b"].excluded_reason == "unpinned"
    assert "c" not in d
    assert d["d"].version == "2.0"
    assert d["E_f"].version == "1.1"
    assert all(x.ecosystem == "pypi" for x in out)


def test_pyproject_pep621(tmp_path):
    out = read(tmp_path, {"pyproject.toml":
        '[project]\nname="x"\ndependencies = ["attrs==24.1.0", "requests>=2"]\n'
        '[project.optional-dependencies]\ndev = ["pytest==8.0.0"]\n'})
    if sys.version_info < (3, 11):
        assert any(d.excluded_reason == "no_tomllib" for d in out)
        pytest.skip("tomllib needs Python 3.11+")
    d = by_name(out)
    assert d["attrs"].version == "24.1.0"
    assert d["requests"].excluded_reason == "unpinned"
    assert d["pytest"].version == "8.0.0"


def test_pyproject_poetry_and_weird_toml(tmp_path):
    if sys.version_info < (3, 11):
        pytest.skip("tomllib needs Python 3.11+")
    out = read(tmp_path, {"pyproject.toml":
        '[tool.poetry.dependencies]\npython="^3.10"\nfoo="1.2.3"\nbar={git="https://x/y.git"}\nbaz="^2"\n'})
    d = by_name(out)
    assert d["foo"].version == "1.2.3" and d["bar"].excluded_reason == "local_or_git"
    assert d["baz"].excluded_reason == "unpinned" and "python" not in d
    out2 = read(tmp_path / "w", {"pyproject.toml": "[project\nthis is = not toml ]]]\n\x00"})
    assert out2 and all(x.excluded_reason in ("unsupported_manifest", "no_tomllib") for x in out2)


def test_local_git_file_specs_excluded(tmp_path):
    out = read(tmp_path, {
        "package.json": json.dumps({"dependencies": {
            "l1": "file:../x", "l2": "git+https://github.com/a/b.git", "l3": "link:../y",
            "l4": "github:a/b", "l5": "a/b", "l6": "https://x.test/p.tgz", "l7": "workspace:*"}}),
        "requirements.txt": "-e .\n-e git+https://github.com/a/b#egg=b\n./local\ngit+https://x/y.git\n"
                            "pkg @ https://x/p.whl\nfile:///tmp/p.whl\n-r other.txt\n--index-url https://i.test\n",
    })
    npm = [d for d in out if d.manifest == "package.json"]
    assert len(npm) == 7 and all(d.excluded_reason == "local_or_git" for d in npm)
    reqs = [d for d in out if d.manifest == "requirements.txt"]
    assert len(reqs) == 6 and all(d.excluded_reason == "local_or_git" for d in reqs)
    assert all(deps.coordinate(d) is None for d in out)


def test_unsupported_manifest_listed(tmp_path):
    out = read(tmp_path, {"go.mod": "module x\n"})
    assert [(d.manifest, d.excluded_reason) for d in out] == [("go.mod", "unsupported_manifest")]


def test_vendored_manifests_not_read(tmp_path):
    out = read(tmp_path, {
        "package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}}),
        "node_modules/is-odd/package.json": json.dumps({"dependencies": {"is-number": "6.0.0"}}),
    })
    assert [d.name for d in out] == ["is-odd"]


def test_coordinates_npm_scoped_and_pypi_normalised():
    assert deps.coordinate(Dependency("npm", "is-odd", "3.0.1", "p", None)) == "npm/npmjs/-/is-odd/3.0.1"
    assert deps.coordinate(Dependency("npm", "@sc/pkg", "1.0.0", "p", None)) == "npm/npmjs/@sc/pkg/1.0.0"
    assert deps.coordinate(Dependency("pypi", "My_Pkg", "1.0", "p", None)) == "pypi/pypi/-/my-pkg/1.0"
    # PEP 503 normalisation (overrides the brief's "lowercase, _ -> -")
    assert deps.coordinate(Dependency("pypi", "Foo_Bar.baz", "1.0", "p", None)) == "pypi/pypi/-/foo-bar-baz/1.0"
    assert deps.coordinate(Dependency("pypi", "a__b--c", "1.0", "p", None)) == "pypi/pypi/-/a-b-c/1.0"
    assert deps.coordinate(Dependency("pypi", "x", None, "p", "unpinned")) is None
    assert deps.coordinate(Dependency("pypi", "x", "1.0", "p", "local_or_git")) is None


@pytest.mark.parametrize("name", [
    "../etc/passwd", "a b", "naïve", "x" * 500, "/home/u/pkg", "C:\\x\\y", "a/b/c", "pkg\x00", "é", "-e",
    "a;b", "..", "@scope", "@/x"])
def test_bad_names_excluded_never_sent(tmp_path, name):
    out = read(tmp_path, {"package.json": json.dumps({"dependencies": {name: "1.0.0"}})})
    assert out and all(deps.coordinate(d) is None for d in out)
    assert all(d.excluded_reason in ("bad_name", "local_or_git") for d in out)
    plan = deps.make_plan(tmp_path)
    assert plan["requests"] == []
    if len(name) > 3:
        assert name not in json.dumps(plan, ensure_ascii=False)


def test_bad_version_excluded(tmp_path):
    out = read(tmp_path, {"requirements.txt": "a==1.0/../../x\nb==" + "9" * 200 + "\n"})
    assert out and all(deps.coordinate(d) is None for d in out)


def test_plan_body_only_coordinates(tmp_path):
    mk(tmp_path, {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1", "rangey": "^1"}}),
                  "requirements.txt": "attrs==24.1.0\nTmp_Home==1.0\n"})
    plan = deps.make_plan(tmp_path)
    assert len(plan["requests"]) == 1
    req = plan["requests"][0]
    assert (req["method"], req["url"]) == ("POST", URL)
    body = json.loads(req["body"])
    assert isinstance(body, list) and all(isinstance(c, str) and re.match(r"^(npm|pypi)/", c) for c in body)
    assert sorted(body) == ["npm/npmjs/-/is-odd/3.0.1", "pypi/pypi/-/attrs/24.1.0", "pypi/pypi/-/tmp-home/1.0"]
    text = json.dumps(plan)
    assert str(tmp_path) not in text and "rangey" not in text
    assert plan["tier"] == "strict" and plan["backend"] == "clearlydefined"
    assert plan["excluded"] and all(set(e) == {"rel", "reason"} for e in plan["excluded"])
    approval.validate_plan(plan)
    assert approval.render_preview(plan)


def test_plan_batches_of_50_and_dedupes(tmp_path):
    reqs = "\n".join(f"pkg{i}==1.0" for i in range(120)) + "\npkg1==1.0\n"
    mk(tmp_path, {"requirements.txt": reqs})
    plan = deps.make_plan(tmp_path)
    sizes = [len(json.loads(r["body"])) for r in plan["requests"]]
    assert sizes == [50, 50, 20] and deps.CD_BATCH == 50


def test_make_plan_no_dependencies(tmp_path):
    mk(tmp_path, {"main.py": "print(1)\n"})
    plan = deps.make_plan(tmp_path)
    assert plan["requests"] == []


def _def_body():
    attrs = json.loads((FIXTURES / "responses" / "cd_attrs_24.1.0.json").read_text(encoding="utf-8"))
    return {"pypi/pypi/-/attrs/24.1.0": attrs}


def test_parse_definition_attrs():
    got = deps.parse_definitions(_def_body())["pypi/pypi/-/attrs/24.1.0"]
    assert got["declared"] == "MIT" and got["known"] is True and got["status"] == "declared"
    assert got["discovered"] == ["MIT"]
    assert "Copyright (c) 2015 Hynek Schlawack" in got["attribution"]
    assert got["release_date"] == "2024-08-03"
    assert got["source_url"] and got["source_url"].startswith("https://pypi.org/")


def test_unknown_definition_is_unknown():
    body = {"npm/npmjs/-/nope/9.9.9": {"licensed": {"toolScore": {"total": 0}}, "scores": {"effective": 0, "tool": 0}},
            "npm/npmjs/-/nada/1.0.0": {}}
    got = deps.parse_definitions(body)
    for c in body:
        assert got[c]["known"] is False and got[c]["status"] == "unknown" and got[c]["declared"] is None


def test_parse_definitions_hostile():
    assert deps.parse_definitions([]) == {} and deps.parse_definitions(None) == {}
    got = deps.parse_definitions({
        "a": "str", "b": {"licensed": [], "scores": "x", "described": 5},
        "c": {"licensed": {"declared": 5, "facets": {"core": {"discovered": {"expressions": "MIT"}}}}}})
    assert set(got) == {"b", "c"} and not got["b"]["known"]


def _plan_file(tmp_path, files):
    mk(tmp_path / "proj", files)
    plan = deps.make_plan(tmp_path / "proj")
    pp = tmp_path / "plan.json"
    approval.write_plan(plan, pp)
    return plan, pp


def test_run_refuses_wrong_hash_without_sending(tmp_path):
    plan, pp = _plan_file(tmp_path, {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}})})

    def boom(*a, **k):
        raise AssertionError("must not send")

    with pytest.raises(approval.ApprovalError) as e:
        deps.run(pp, "0" * 64, opener=boom)
    assert e.value.code == "plan_hash_mismatch"
    out = tmp_path / "res.json"
    assert deps.main(["run", str(pp), "--sha256", "0" * 64, "--out", str(out)]) == 2
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["errors"][0]["code"] == "plan_hash_mismatch"


def test_run_c6_replay(tmp_path):
    plan = deps.make_plan(FIXTURES / "cases" / "C6")
    assert json.loads(plan["requests"][0]["body"]) == ["npm/npmjs/-/is-odd/3.0.1"]
    pp = tmp_path / "plan.json"
    approval.write_plan(plan, pp)
    res = deps.run(pp, plan["sha256"], opener=ReplayOpener(FIXTURES / "responses"))
    assert res["errors"] == [] and res["not_sent"] == []
    (dep,) = res["dependencies"]
    assert (dep["ecosystem"], dep["name"], dep["version"]) == ("npm", "is-odd", "3.0.1")
    assert dep["manifest"] == "package.json"
    assert dep["license"]["spdx"] == "MIT" and dep["license"]["evidence"] == "clearlydefined"
    assert dep["license"]["status"] == "declared" and dep["known"] is True
    assert len(res["requests_sent"]) == 1


class FakeOpener:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status, self.body, self.headers, self.calls = status, body, headers or {}, 0

    def open(self, req, timeout=None):
        self.calls += 1
        raise urllib.error.HTTPError(req.full_url, self.status, "x", self.headers, io.BytesIO(self.body))

    __call__ = open


class OkOpener:
    def __init__(self, body):
        self.body = body

    def open(self, req, timeout=None):
        body = self.body
        return type("R", (), {"status": 200, "headers": {}, "read": lambda s, n=-1: body,
                              "getcode": lambda s: 200, "close": lambda s: None})()

    __call__ = open


def test_run_429_becomes_coverage_error(tmp_path):
    plan, pp = _plan_file(tmp_path, {"requirements.txt": "\n".join(f"p{i}==1.0" for i in range(120))})
    op = FakeOpener(429)
    res = deps.run(pp, plan["sha256"], opener=op, sleep=lambda s: None)
    assert op.calls == 3                       # one try plus two retries; nothing after quota_exhausted
    assert res["errors"][0]["code"] == "quota_exhausted"
    assert len(res["not_sent"]) == 3 and res["not_sent"][0]["id"] == plan["requests"][0]["id"]
    assert res["dependencies"] and all(d["license"]["status"] == "unknown" and d["known"] is False
                                       for d in res["dependencies"])


def test_run_bad_response_is_error_not_clean(tmp_path):
    plan, pp = _plan_file(tmp_path, {"requirements.txt": "a==1.0\n"})
    res = deps.run(pp, plan["sha256"], opener=OkOpener(b"not json"))
    assert res["errors"][0]["code"] == "bad_response" and len(res["not_sent"]) == 1


def test_run_http_500_is_error(tmp_path):
    plan, pp = _plan_file(tmp_path, {"requirements.txt": "a==1.0\n"})
    res = deps.run(pp, plan["sha256"], opener=FakeOpener(500), sleep=lambda s: None)
    assert res["errors"] and res["errors"][0]["code"] != "quota_exhausted" and res["not_sent"]


def test_cli_run_error_exit_and_results_file(tmp_path, capsys):
    plan, pp = _plan_file(tmp_path, {"requirements.txt": "a==1.0\n"})
    out = tmp_path / "r.json"
    # the CLI uses the real opener; sockets are blocked, so the request fails as a network error
    rc = deps.main(["run", str(pp), "--sha256", plan["sha256"], "--out", str(out)])
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 2 and "error" in printed
    assert json.loads(out.read_text(encoding="utf-8"))["errors"]


def test_cli_plan_prints_preview(tmp_path, capsys):
    mk(tmp_path / "proj", {"package.json": json.dumps({"dependencies": {"is-odd": "3.0.1"}})})
    out = tmp_path / "p.json"
    assert deps.main(["plan", str(tmp_path / "proj"), "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["sha256"] and "npm/npmjs/-/is-odd/3.0.1" in printed["preview"]
    assert deps.main(["plan", str(tmp_path / "missing"), "--out", str(out)]) == 2


# ---- hostile input, each under a time budget --------------------------------------

def _timed(fn):
    t = time.perf_counter()
    r = fn()
    assert time.perf_counter() - t < 2.0
    return r


def test_hostile_huge_package_json(tmp_path):
    mk(tmp_path, {"package.json": b'{"dependencies": {"a": "1.0.0"}, "pad": "' + b"x" * (50 * 1024 * 1024) + b'"}'})
    out = _timed(lambda: deps.read_dependencies(inventory(tmp_path, require_sources=False)))
    assert [d.excluded_reason for d in out] == ["unsupported_manifest"]


def test_hostile_deeply_nested_json(tmp_path):
    mk(tmp_path, {"package.json": "[" * 200000 + "]" * 200000, "package-lock.json": '{"a":' * 100000})
    out = _timed(lambda: deps.read_dependencies(inventory(tmp_path, require_sources=False)))
    assert out and all(d.excluded_reason == "unsupported_manifest" for d in out)


def test_hostile_invalid_json_and_wrong_shapes(tmp_path):
    mk(tmp_path, {"package.json": "{not json", "package-lock.json": "[1,2,3]"})
    out = _timed(lambda: deps.read_dependencies(inventory(tmp_path, require_sources=False)))
    assert out and all(d.excluded_reason == "unsupported_manifest" for d in out)
    mk(tmp_path / "w", {"package.json": json.dumps(
        {"dependencies": ["a", 1, None], "devDependencies": {"x": 5, "y": None}})})
    _timed(lambda: deps.read_dependencies(inventory(tmp_path / "w", require_sources=False)))


def test_hostile_non_utf8_requirements(tmp_path):
    mk(tmp_path, {"requirements.txt": b"a==1.0\n\xff\xfe\x80bad==2.0\nb\xe9==1.0\nc==3.0\n"})
    out = _timed(lambda: deps.read_dependencies(inventory(tmp_path, require_sources=False)))
    d = by_name(out)
    assert d["a"].version == "1.0" and d["c"].version == "3.0"
    assert all(deps.coordinate(x) is None for x in out if x.name not in ("a", "c"))


def test_requirements_utf16_with_bom(tmp_path):
    out = read(tmp_path, {"requirements.txt": "attrs==24.1.0\r\nflask==3.0.0\r\n".encode("utf-16")})
    assert by_name(out)["attrs"].version == "24.1.0"


def test_hostile_100k_line_requirements(tmp_path):
    lines = "\n".join(f"pkg-{i}==1.{i}" if i % 3 else f"junk {i} >=<" for i in range(100_000))
    mk(tmp_path, {"requirements.txt": lines})

    def go():
        inv = inventory(tmp_path, require_sources=False)
        return deps.read_dependencies(inv), deps.make_plan(tmp_path)

    out, plan = _timed(go)
    assert len(out) <= deps.MAX_DEPENDENCIES + 1
    assert sum(len(json.loads(r["body"])) for r in plan["requests"]) <= deps.MAX_DEPENDENCIES
    assert any(d.excluded_reason == "too_many" for d in out)


def test_hostile_big_toml(tmp_path):
    if sys.version_info < (3, 11):
        pytest.skip("tomllib needs Python 3.11+")
    big = "[project]\ndependencies = [" + ",".join(f'"p{i}==1.0"' for i in range(20000)) + "]\n"
    mk(tmp_path, {"pyproject.toml": big})
    out = _timed(lambda: deps.read_dependencies(inventory(tmp_path, require_sources=False)))
    assert out
