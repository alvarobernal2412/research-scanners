"""Acceptance cases LC1-LC12 against fixture projects and a fake registry (no network)."""
import io
import json
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

import check_licenses
import lookup_registry

MIT_TEXT = ("MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy "
            "of this software.")
SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "license-compliance" / "scripts" / "check_licenses.py"


class FakeResp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class FakeRegistry:
    """Answers PyPI and npm URLs from {name: license}; a value of None means 'no license metadata'."""

    def __init__(self, pypi=None, npm=None, fail=False):
        self.pypi, self.npm, self.fail = pypi or {}, npm or {}, fail
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if self.fail:
            raise urllib.error.URLError("down")
        m = re.match(r"https://pypi\.org/pypi/([^/]+)/(?:[^/]+/)?json$", url)
        if m:
            name = m.group(1)
            if name not in self.pypi:
                raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO(b""))
            lic = self.pypi[name]
            info = {"license_expression": lic} if lic else {"license": None, "classifiers": []}
            return FakeResp(json.dumps({"info": info}).encode())
        m = re.match(r"https://registry\.npmjs\.org/([^/]+)/", url)
        if m:
            name = m.group(1).replace("%2F", "/")
            if name not in self.npm:
                raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO(b""))
            return FakeResp(json.dumps({"license": self.npm[name]} if self.npm[name] else {}).encode())
        raise AssertionError(f"unexpected url {url}")


def project(tmp_path, requirements, license_text=MIT_TEXT):
    (tmp_path / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
    if license_text:
        (tmp_path / "LICENSE").write_text(license_text, encoding="utf-8")
    return tmp_path


def run(tmp_path, registry, **kw):
    return check_licenses.build_report(tmp_path, opener=registry, **kw)


def verdict(report, name):
    return next(d for d in report["dependencies"] if d["name"] == name)["verdict"]


def test_lc1_all_permissive(tmp_path):
    project(tmp_path, ["a==1", "b==1"])
    rep = run(tmp_path, FakeRegistry(pypi={"a": "MIT", "b": "Apache-2.0"}))
    assert verdict(rep, "a")["status"] == "compatible" and verdict(rep, "b")["status"] == "compatible"
    assert rep["recommendation"] is None and rep["summary"]["conflicts"] == 0
    assert rep["coverage"]  # never a bare "clean"
    assert "clean" not in json.dumps(rep["summary"])


def test_lc2_gpl3_in_mit_project(tmp_path):
    project(tmp_path, ["g==1"])
    rep = run(tmp_path, FakeRegistry(pypi={"g": "GPL-3.0-only"}))
    assert verdict(rep, "g")["status"] == "conflict"
    assert rep["conflicts"][0]["name"] == "g"
    assert rep["recommendation"]["recommended"] == "GPL-3.0-or-later"  # suggestion to relicense


def test_lc3_apache_project_gpl2_only_dep(tmp_path):
    project(tmp_path, ["g==1"], "Apache License\nVersion 2.0, January 2004")
    rep = run(tmp_path, FakeRegistry(pypi={"g": "GPL-2.0-only"}))
    assert rep["project"]["license"] == "Apache-2.0"
    assert verdict(rep, "g")["status"] == "conflict"


def test_lc4_gpl3_project_with_permissive_and_lgpl(tmp_path):
    project(tmp_path, ["a==1", "b==1", "c==1"], "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007")
    rep = run(tmp_path, FakeRegistry(pypi={"a": "MIT", "b": "Apache-2.0", "c": "LGPL-3.0-only"}))
    assert {verdict(rep, n)["status"] for n in "abc"} == {"compatible"}


def test_lc5_no_license_permissive_deps(tmp_path):
    project(tmp_path, ["a==1", "b==1"], license_text=None)
    rep = run(tmp_path, FakeRegistry(pypi={"a": "MIT", "b": "Apache-2.0"}))
    assert rep["project"]["license"] is None
    assert rep["recommendation"]["recommended"] == "Apache-2.0"
    assert rep["recommendation"]["not_usable"] == []
    assert rep["judged_against"]["basis"] == "recommended"


def test_lc6_no_license_gpl_dep_lists_unusable(tmp_path):
    project(tmp_path, ["g==1"], license_text=None)
    rep = run(tmp_path, FakeRegistry(pypi={"g": "GPL-3.0-only"}))
    assert rep["recommendation"]["recommended"] == "GPL-3.0-or-later"
    out = {n["license"]: n for n in rep["recommendation"]["not_usable"]}
    for lic in ("MIT", "BSD-3-Clause", "Apache-2.0", "MPL-2.0"):
        assert out[lic]["blocked_by"][0]["dependency"] == "g"


def test_lc7_dependency_without_license(tmp_path):
    project(tmp_path, ["x==1"])
    rep = run(tmp_path, FakeRegistry(pypi={"x": None}))
    v = verdict(rep, "x")
    assert v["status"] == "needs_review" and v["reason"] == "no_license"
    assert [n["name"] for n in rep["not_usable_as_is"]] == ["x"]


def test_lc8_or_expression(tmp_path):
    project(tmp_path, ["d==1"])
    rep = run(tmp_path, FakeRegistry(pypi={"d": "MIT OR GPL-3.0-only"}))
    assert verdict(rep, "d")["status"] == "compatible"


def test_lc9_dev_dependency_not_judged(tmp_path):
    project(tmp_path, ["a==1"])
    (tmp_path / "requirements-dev.txt").write_text("g==1\n", encoding="utf-8")
    reg = FakeRegistry(pypi={"a": "MIT", "g": "GPL-3.0-only"})
    rep = run(tmp_path, reg)
    assert verdict(rep, "g")["status"] == "not_judged"
    assert rep["summary"]["conflicts"] == 0
    assert not any("/g/" in u for u in reg.urls)  # dev dependency not even looked up
    assert any("development-only" in c for c in rep["coverage"])


def test_lc10_unregistered_source_never_sent(tmp_path):
    project(tmp_path, ["a==1", "-e git+https://github.com/me/secret-internal.git#egg=secret-internal"])
    reg = FakeRegistry(pypi={"a": "MIT"})
    rep = run(tmp_path, reg)
    v = verdict(rep, "secret-internal")
    assert v["status"] == "needs_review" and v["reason"] == "unregistered_source"
    assert not any("secret" in u for u in reg.urls)


def test_lc11_registry_down(tmp_path):
    project(tmp_path, ["a==1"])
    rep = run(tmp_path, FakeRegistry(fail=True))
    v = verdict(rep, "a")
    assert v["status"] == "needs_review" and v["reason"] == "lookup_failed"
    assert any("lookup failed" in c.lower() for c in rep["coverage"])
    assert rep["summary"]["result"] != "conflicts found"


def test_lc12_offline_uses_local_metadata_and_no_requests(tmp_path):
    project(tmp_path, ["pkg==1.0", "other==2.0"])
    meta = tmp_path / ".venv" / "Lib" / "site-packages" / "pkg-1.0.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text("Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nLicense-Expression: MIT\n\nbody",
                                   encoding="utf-8")
    reg = FakeRegistry()
    rep = run(tmp_path, reg, offline=True)
    assert reg.urls == [] and rep["requests_log"] == []
    assert verdict(rep, "pkg")["status"] == "compatible"
    assert verdict(rep, "other")["status"] == "needs_review"


def test_only_names_and_versions_are_sent(tmp_path):
    project(tmp_path, ["a==1.2.3"])
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"@scope/b": "2.0.0"}}), encoding="utf-8")
    reg = FakeRegistry(pypi={"a": "MIT"}, npm={"@scope/b": "ISC"})
    rep = run(tmp_path, reg)
    assert sorted(reg.urls) == ["https://pypi.org/pypi/a/1.2.3/json", "https://registry.npmjs.org/@scope%2Fb/2.0.0"]
    assert [r["url"] for r in rep["requests_log"]] == reg.urls
    assert verdict(rep, "@scope/b")["status"] == "compatible"


def test_ambiguous_classifiers_flagged(tmp_path):
    project(tmp_path, ["m==1"])

    class Reg(FakeRegistry):
        def __call__(self, req, timeout=None):
            self.urls.append(req.full_url)
            info = {"classifiers": ["License :: OSI Approved :: MIT License",
                                    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)"]}
            return FakeResp(json.dumps({"info": info}).encode())

    rep = run(tmp_path, Reg())
    assert verdict(rep, "m")["reason"] == "dual_license_ambiguous"


def test_no_manifests_is_an_error(tmp_path):
    assert check_licenses.build_report(tmp_path, offline=True) == {"error": "no_manifests_found"}


def test_cli_offline(tmp_path):
    project(tmp_path, ["a==1"])
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(tmp_path), "--offline"],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["requests_log"] == [] and out["dependencies"][0]["verdict"]["status"] == "needs_review"


def test_cli_not_a_directory(tmp_path):
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(tmp_path / "nope")], capture_output=True, text=True)
    assert proc.returncode == 2 and json.loads(proc.stdout) == {"error": "not_a_directory"}
