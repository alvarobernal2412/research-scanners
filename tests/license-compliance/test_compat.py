import pytest

import compat
import recommend


@pytest.mark.parametrize("dep,proj,status", [
    ("MIT", "MIT", "compatible"),
    ("MIT", "GPL-3.0-or-later", "compatible"),
    ("MIT", "LicenseRef-Proprietary", "compatible"),
    ("GPL-3.0-only", "MIT", "conflict"),
    ("GPL-3.0-or-later", "GPL-3.0-or-later", "compatible"),
    ("GPL-3.0-only", "AGPL-3.0-or-later", "compatible"),
    ("AGPL-3.0-only", "GPL-3.0-only", "conflict"),
    ("GPL-2.0-only", "Apache-2.0", "conflict"),
    ("GPL-2.0-only", "GPL-3.0-or-later", "conflict"),
    ("Apache-2.0", "GPL-2.0-only", "conflict"),
    ("Apache-2.0", "GPL-3.0-or-later", "compatible"),
    ("LGPL-3.0-or-later", "MIT", "compatible"),
    ("LGPL-3.0-only", "GPL-2.0-only", "conflict"),
    ("MPL-2.0", "GPL-3.0-or-later", "compatible"),
    ("EPL-2.0", "GPL-3.0-or-later", "conflict"),
    ("CC-BY-NC-4.0", "MIT", "needs_review"),
    ("LicenseRef-Proprietary", "MIT", "needs_review"),
])
def test_pairs(dep, proj, status):
    assert compat.judge(dep, proj)["status"] == status


def test_no_license_never_permissive():
    res = compat.judge(None, "MIT")
    assert res["status"] == "needs_review" and res["reason"] == "no_license"


def test_unparseable_license():
    assert compat.judge("Weird Custom Terms", "MIT")["reason"] == "unknown_license"


def test_or_and_with():
    assert compat.judge("MIT OR GPL-3.0-only", "MIT")["status"] == "compatible"
    assert compat.judge("GPL-3.0-only AND MIT", "MIT")["status"] == "conflict"
    assert compat.judge("GPL-2.0-only WITH Classpath-exception-2.0", "MIT")["status"] == "compatible"
    assert compat.judge("GPL-2.0-only WITH Classpath-exception-2.0", "MIT")["conditions"]


def test_dual_licensed_project_needs_every_option():
    assert compat.judge("Apache-2.0", "MIT OR GPL-2.0-only")["status"] == "conflict"
    assert compat.judge("MIT", "MIT OR Apache-2.0")["status"] == "compatible"


def deps_of(*pairs):
    return [{"name": n, "version": "1", "license": lic} for n, lic in pairs]


def test_recommend_all_permissive_prefers_apache():
    rec = recommend.recommend(deps_of(("a", "MIT"), ("b", "Apache-2.0")))
    assert rec["recommended"] == "Apache-2.0" and rec["not_usable"] == []
    assert any("patent" in c for c in rec["caveats"])


def test_recommend_gpl3_forces_copyleft():
    rec = recommend.recommend(deps_of(("a", "MIT"), ("g", "GPL-3.0-only")))
    assert rec["recommended"] == "GPL-3.0-or-later"
    out = {n["license"]: n for n in rec["not_usable"]}
    for lic in ("MIT", "Apache-2.0", "BSD-3-Clause", "MPL-2.0"):
        assert lic in out and out[lic]["blocked_by"][0]["dependency"] == "g"


def test_recommend_gpl2_only_excludes_apache_and_gpl3():
    rec = recommend.recommend(deps_of(("g", "GPL-2.0-only")))
    usable = [u["license"] for u in rec["usable"]]
    assert usable == ["GPL-2.0-or-later"]


def test_recommend_impossible():
    rec = recommend.recommend(deps_of(("g2", "GPL-2.0-only"), ("g3", "GPL-3.0-only")))
    assert rec["recommended"] is None and any("No candidate" in c for c in rec["caveats"])


def test_recommend_skips_unknown_but_reports_them():
    rec = recommend.recommend(deps_of(("a", "MIT"), ("x", None)))
    assert rec["unresolved_dependencies"] == [{"dependency": "x", "license": None}]
    assert rec["recommended"] == "Apache-2.0"
    assert any("provisional" in c for c in rec["caveats"])
