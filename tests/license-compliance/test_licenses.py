import json

import pytest

import licenses

MIT_TEXT = """MIT License

Copyright (c) 2026 Someone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction."""


@pytest.mark.parametrize("raw,expected", [
    ("MIT", "MIT"),
    ("mit", "MIT"),
    ("MIT License", "MIT"),
    ("Apache 2.0", "Apache-2.0"),
    ("Apache Software License", "Apache-2.0"),
    ("License :: OSI Approved :: MIT License", "MIT"),
    ("License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)", "GPL-3.0-or-later"),
    ("GPL-3.0", "GPL-3.0-only"),
    ("GPL-2.0+", "GPL-2.0-or-later"),
    ("LGPL-3.0-or-later", "LGPL-3.0-or-later"),
    ("BSD", "BSD-3-Clause"),
    ("MIT OR Apache-2.0", "MIT OR Apache-2.0"),
    ("MIT/Apache-2.0", "MIT OR Apache-2.0"),
    ("(MIT AND BSD-3-Clause)", "MIT AND BSD-3-Clause"),
    ("GPL-2.0-only WITH Classpath-exception-2.0", "GPL-2.0-only WITH Classpath-exception-2.0"),
])
def test_normalize(raw, expected):
    assert licenses.normalize(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "UNKNOWN", "Other/Proprietary", "Totally Made Up License 9"])
def test_normalize_unusable(raw):
    assert licenses.normalize(raw) is None


def test_expression_tree():
    node = licenses.parse_expression("MIT OR (GPL-3.0-only AND Apache-2.0)")
    assert node[0] == "or"
    assert node[1][0] == ("id", "MIT", None)
    assert node[1][1][0] == "and"
    assert licenses.parse_expression("MIT OR") is None


def test_detect_from_text():
    assert licenses.detect_from_text(MIT_TEXT) == "MIT"
    assert licenses.detect_from_text("GNU AFFERO GENERAL PUBLIC LICENSE Version 3") == "AGPL-3.0-only"
    assert licenses.detect_from_text("Apache License\n Version 2.0, January 2004") == "Apache-2.0"
    assert licenses.detect_from_text("nothing useful") is None


def test_project_license_from_file(tmp_path):
    (tmp_path / "LICENSE").write_text(MIT_TEXT, encoding="utf-8")
    res = licenses.detect_project_license(tmp_path)
    assert res["spdx"] == "MIT"
    assert res["evidence"][0]["source"] == "LICENSE"


def test_project_license_none(tmp_path):
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    res = licenses.detect_project_license(tmp_path)
    assert res["spdx"] is None and res["evidence"] == []


def test_project_license_package_json_and_disagreement(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "license": "ISC"}), encoding="utf-8")
    (tmp_path / "LICENSE").write_text(MIT_TEXT, encoding="utf-8")
    res = licenses.detect_project_license(tmp_path)
    assert res["spdx"] == "MIT"  # license file wins
    assert any("disagree" in n for n in res["notes"])


def test_project_license_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nlicense = "Apache-2.0"\n', encoding="utf-8")
    assert licenses.detect_project_license(tmp_path)["spdx"] == "Apache-2.0"


def test_unrecognised_license_file_noted(tmp_path):
    (tmp_path / "LICENSE.txt").write_text("Do what you like, mostly.", encoding="utf-8")
    res = licenses.detect_project_license(tmp_path)
    assert res["spdx"] is None
    assert any("matched no known license" in n for n in res["notes"])


@pytest.mark.parametrize("raw", ["GPL", "LGPL", "AGPL"])
def test_unversioned_gnu_names_are_not_guessed(raw):
    assert licenses.normalize(raw) is None
