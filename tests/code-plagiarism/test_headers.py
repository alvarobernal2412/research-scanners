import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

SKILL = Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism"
sys.path.insert(0, str(SKILL / "scripts"))

import headers  # noqa: E402
from headers import (detect_header, detect_license_file, effective_file_license,  # noqa: E402
                     find_references, project_licenses, run_scancode)
from inventory import inventory, read_lines  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
CASES = FIX / "cases"

ANSIBLE = """\
# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
""".splitlines()


def test_spdx_tag_line_1():
    ev = detect_header(read_lines(next((CASES / "C4").glob("*.py"))))
    tag = [e for e in ev if e.kind == "spdx_tag"]
    assert len(tag) == 1
    assert (tag[0].spdx, tag[0].line, tag[0].confidence) == ("MIT", 1, "high")


def test_spdx_comment_forms_and_case():
    for line in ("// SPDX-License-Identifier: mit", "/* SPDX-License-Identifier: MIT */",
                 "<!-- SPDX-License-Identifier: MIT -->"):
        ev = detect_header([line])
        assert [(e.spdx, e.kind) for e in ev] == [("MIT", "spdx_tag")]


def test_unknown_spdx_kept_medium():
    ev = detect_header(["# SPDX-License-Identifier: Zlib"])
    assert (ev[0].spdx, ev[0].confidence) == ("Zlib", "medium")


def test_gpl3_or_later_header_text():
    ev = detect_header(ANSIBLE)
    lic = [e for e in ev if e.kind == "license_text"]
    assert [e.spdx for e in lic] == ["GPL-3.0-or-later"]
    assert lic[0].source == "file_header"


def test_released_under_mit_header():
    ev = detect_header(read_lines(CASES / "C6/node_modules/is-odd/index.js"))
    assert "MIT" in [e.spdx for e in ev if e.kind == "license_text"]


def test_copyright_line_detected():
    ev = detect_header(["// Copyright (c) 2020 Jane Doe", "x = 1"])
    assert [(e.kind, e.spdx, e.line) for e in ev] == [("copyright", None, 1)]


def test_license_file_signatures():
    for p in (CASES / "C6/node_modules/is-odd/LICENSE", CASES / "C5/LICENSE"):
        ev = detect_license_file("\n".join(read_lines(p)))
        assert ev is not None and ev.spdx == "MIT" and ev.source == "repo_license"
    assert detect_license_file("hello world") is None


def test_headerless_gpl_function_has_no_evidence():
    assert detect_header(read_lines(CASES / "C5/C5_ansible_unfrackpath_GPL3.py")) == []


def test_stackoverflow_reference_variants():
    urls = [
        "https://stackoverflow.com/a/58437909",
        "https://stackoverflow.com/questions/35038857/title/58437909#58437909",
        "https://stackoverflow.com/posts/58437909/revisions/58437909/17",
        "https://stackoverflow.com/revisions/58437909/17",
    ]
    for u in urls:
        refs = find_references([f"// see {u}."])
        assert len(refs) == 1 and refs[0].kind == "stackoverflow" and refs[0].post_id == 58437909, u
    refs = find_references(read_lines(next((CASES / "C3").glob("*.js"))))
    assert refs[0].post_id == 58437909 and refs[0].line == 1


def test_github_reference():
    refs = find_references(["# from https://github.com/psf/requests/blob/main/x.py#L10",
                            "# http://example.com/a"])
    assert [(r.kind, r.post_id) for r in refs] == [("github", None), ("url", None)]
    assert refs[1].line == 2


def test_per_file_header_beats_repo_license(tmp_path):
    (tmp_path / "LICENSE").write_text((CASES / "C5/LICENSE").read_text(encoding="utf-8"),
                                      encoding="utf-8")
    (tmp_path / "a.py").write_text("# SPDX-License-Identifier: GPL-2.0-only\nx = 1\n",
                                   encoding="utf-8")
    proj = project_licenses(inventory(tmp_path))
    assert proj and proj[0].spdx == "MIT"
    eff = effective_file_license(detect_header(read_lines(tmp_path / "a.py")), proj)
    assert (eff.spdx, eff.source) == ("GPL-2.0-only", "file_header")
    assert effective_file_license([], proj).spdx == "MIT"
    assert effective_file_license([], []) is None


def test_project_licenses_manifest(tmp_path):
    (tmp_path / "package.json").write_bytes((CASES / "C6/package.json").read_bytes())
    (tmp_path / "a.js").write_text("var a = 1;\n", encoding="utf-8")
    ev = project_licenses(inventory(tmp_path))
    assert any(e.source == "package_manifest" and e.spdx == "MIT" for e in ev)


def test_scancode_not_installed(monkeypatch):
    monkeypatch.setattr(headers, "find_exe", lambda *_a, **_k: None)
    assert headers.scancode_status() == "not_installed"
    assert run_scancode(Path("."), ["a.py"]) is None


def test_scancode_output_parsed(monkeypatch, tmp_path):
    monkeypatch.setattr(headers, "find_exe", lambda *_a, **_k: "scancode")
    sample = (FIX / "responses/scancode_sample.json").read_text(encoding="utf-8")
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("--json") + 1]).write_text(sample, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    out = run_scancode(tmp_path, ["C4_attrs_filters_MIT.py"], runner=runner)
    assert headers.scancode_status() == "available"
    assert list(out) == ["C4_attrs_filters_MIT.py"]
    ev = out["C4_attrs_filters_MIT.py"]
    assert ev[0].spdx == "MIT" and ev[0].source == "scancode"
    assert seen["cmd"][1:3] == ["-l", "-c"] and "-n" in seen["cmd"]


def test_signatures_cover_required_ids():
    sigs = json.loads((SKILL / "data/license_signatures.json").read_text(encoding="utf-8"))
    need = {"MIT", "Apache-2.0", "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only",
            "GPL-3.0-or-later", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0", "BSD-2-Clause",
            "BSD-3-Clause", "MPL-2.0", "ISC", "Unlicense", "CC-BY-SA-3.0", "CC-BY-SA-4.0",
            "CC-BY-4.0", "CC0-1.0"}
    assert need <= set(sigs)


def _timed(fn, budget=2.0):
    import time
    t = time.perf_counter()
    fn()
    assert time.perf_counter() - t < budget


def test_redos_whitespace_run_in_spdx_tag():
    _timed(lambda: detect_header(["# SPDX-License-Identifier:" + " " * 200_000 + "x"]))


def test_redos_whitespace_run_old_pattern_size():
    _timed(lambda: headers._SPDX_TAG.search("SPDX-License-Identifier:" + " " * 20_000 + "x"))


def test_many_token_spdx_value_is_fast():
    _timed(lambda: detect_header(["// SPDX-License-Identifier: " + "a " * 100_000]))


def test_five_mb_single_line_is_fast():
    line = "// Copyright " + "a" * (5 * 1024 * 1024)
    _timed(lambda: detect_header([line, line]))


def test_long_spdx_id_truncated():
    ev = detect_header(["# SPDX-License-Identifier: " + "Z" * 900])
    assert len(ev[0].spdx) <= 160


def _ev(spdx, kind, conf, source="file_header"):
    return headers.LicenseEvidence(spdx, kind, 1, "t", source, conf)


def test_medium_prose_does_not_displace_repo_licence():
    repo = _ev("MIT", "license_text", "high", "repo_license")
    for prose in (_ev("MIT", "license_text", "medium"), _ev("Apache-2.0", "license_text", "medium"),
                  _ev("Zlib", "spdx_tag", "medium")):
        eff = effective_file_license([prose], [repo])
        assert (eff.spdx, eff.source, eff.confidence) == ("MIT", "repo_license", "high")


def test_medium_prose_used_when_no_repo_licence():
    prose = _ev("Apache-2.0", "license_text", "medium")
    assert effective_file_license([prose], []) is prose


def test_spdx_tag_apache_beats_repo_mit():
    tag = _ev("Apache-2.0", "spdx_tag", "high")
    repo = _ev("MIT", "license_text", "high", "repo_license")
    assert effective_file_license([tag], [repo]).spdx == "Apache-2.0"


GPL2_NOTICE = """\
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
""".splitlines()

HEADER_SAMPLES = {
    "MIT": "Permission is hereby granted, free of charge, to any person obtaining a copy of this software",
    "Apache-2.0": "Licensed under the Apache License, Version 2.0 (the \"License\");",
    "GPL-2.0-only": "under the terms of the GNU General Public License version 2 of the License.",
    "GPL-2.0-or-later": "GNU General Public License as published by the Free Software Foundation; "
                        "either version 2 of the License, or (at your option) any later version.",
    "GPL-3.0-only": "under the terms of the GNU General Public License, version 3 of the License.",
    "GPL-3.0-or-later": "GNU General Public License as published by the Free Software Foundation, "
                        "either version 3 of the License, or (at your option) any later version.",
    "LGPL-2.1": "GNU Lesser General Public License as published by the Free Software Foundation; "
                "either version 2.1 of the License",
    "LGPL-3.0": "GNU Lesser General Public License as published by the Free Software Foundation, "
                "either version 3 of the License",
    "AGPL-3.0": "GNU Affero General Public License as published by the Free Software Foundation",
    "BSD-2-Clause": "Redistribution and use in source and binary forms, with or without modification, "
                    "are permitted provided that the following conditions are met:",
    "BSD-3-Clause": "Redistribution and use in source and binary forms, with or without modification, "
                    "are permitted. Neither the name of the copyright holder nor the names",
    "MPL-2.0": "This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.",
    "ISC": "Permission to use, copy, modify, and/or distribute this software for any purpose "
           "with or without fee is hereby granted",
    "Unlicense": "This is free and unencumbered software released into the public domain.",
    "CC-BY-SA-3.0": "This work is licensed under a Creative Commons Attribution-ShareAlike 3.0 licence",
    "CC-BY-SA-4.0": "This work is licensed under the Creative Commons Attribution-ShareAlike 4.0 licence",
    "CC-BY-4.0": "This work is licensed under a Creative Commons Attribution 4.0 International licence",
    "CC0-1.0": "To the extent possible under law, the person who associated CC0 with this work has waived",
}


def test_signature_strengths_and_header_samples():
    sigs = json.loads((SKILL / "data/license_signatures.json").read_text(encoding="utf-8"))
    for sid, groups in sigs.items():
        assert set(groups) <= {"header", "mention"} and (groups.get("header") or groups.get("mention")), sid
    header_ids = {sid for sid, g in sigs.items() if g.get("header")}
    assert header_ids <= set(HEADER_SAMPLES)
    for sid in header_ids:
        ev = [e for e in detect_header(["# " + HEADER_SAMPLES[sid]]) if e.spdx == sid]
        assert ev and ev[0].confidence == "high" and ev[0].source == "file_header", sid


def test_full_gpl_notice_beats_repo_mit():
    ev = detect_header(GPL2_NOTICE)
    gpl = [e for e in ev if e.kind == "license_text"]
    assert [(e.spdx, e.confidence, e.source) for e in gpl] == [("GPL-2.0-or-later", "high", "file_header")]
    repo = _ev("MIT", "license_text", "high", "repo_license")
    eff = effective_file_license(ev, [repo])
    assert (eff.spdx, eff.confidence) == ("GPL-2.0-or-later", "high")


def test_full_mit_notice_is_high():
    ev = detect_header(["/*", " * Permission is hereby granted, free of charge, to any person obtaining a copy", " */"])
    assert [(e.spdx, e.confidence) for e in ev if e.kind == "license_text"] == [("MIT", "high")]


def test_bare_mentions_stay_medium_and_do_not_displace_repo():
    repo = _ev("Apache-2.0", "license_text", "high", "repo_license")
    for line in ("# MIT License", "# see the MIT license file"):
        ev = detect_header([line])
        assert [(e.spdx, e.confidence) for e in ev] == [("MIT", "medium")], line
        assert effective_file_license(ev, [repo]).spdx == "Apache-2.0"
