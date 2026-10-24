"""Report assembly: merges local analysis and backend results files; makes no network calls."""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"))

import approval  # noqa: E402
import deps  # noqa: E402
import report  # noqa: E402
import scanoss_backend as sb  # noqa: E402
import stackexchange as se  # noqa: E402
from model import Candidate, RawMatch, new_results, raw_match_to_dict, write_results  # noqa: E402
from replay import ReplayOpener, ScanossReplayRunner  # noqa: E402
from test_scanoss_backend import FakeRunner  # noqa: E402
from test_stackexchange import FakeSE, hit_c1  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
CASES = FIXTURES / "cases"
RESPONSES = FIXTURES / "responses"
BY_MD5 = RESPONSES / "scanoss_by_md5.json"

FORBIDDEN = ("infring", "violat", "illegal", "guilty", "clean", "stole", "stolen")


# ---- builders ------------------------------------------------------------------------

def project(tmp_path, files):
    """files: {rel: fixture Path | str | bytes}. Fixture files are copied byte for byte."""
    root = tmp_path / "proj"
    for rel, src in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(src, Path):
            shutil.copyfile(src, dest)
        else:
            dest.write_bytes(src if isinstance(src, bytes) else src.encode("utf-8"))
    return root


def scanoss_results(tmp_path, root, includes=("src",)):
    plan = sb.make_plan(root, list(includes), runner=FakeRunner())
    pp = tmp_path / "sb_plan.json"
    approval.write_plan(plan, pp)
    res = sb.run(pp, plan["sha256"], runner=FakeRunner(scan=ScanossReplayRunner(BY_MD5)))
    out = tmp_path / "sb_results.json"
    write_results(out, res)
    return out


def se_results(tmp_path, root, includes=("src",), search=hit_c1, **fake_kw):
    plan = se.make_plan(root, list(includes))
    pp = tmp_path / "se_plan.json"
    approval.write_plan(plan, pp)
    res = se.run(pp, plan["sha256"], root, opener=FakeSE(search=search, **fake_kw), sleep=lambda s: None)
    out = tmp_path / "se_results.json"
    write_results(out, res)
    return out


def cd_results(tmp_path, root):
    plan = deps.make_plan(root)
    pp = tmp_path / "cd_plan.json"
    approval.write_plan(plan, pp)
    res = deps.run(pp, plan["sha256"], opener=ReplayOpener(RESPONSES), sleep=lambda s: None)
    out = tmp_path / "cd_results.json"
    write_results(out, res)
    return out


def hand_results(tmp_path, root, matches, backend="scanoss", tier="fingerprint", name="hand.json", **extra):
    res = new_results(backend, tier, "a" * 64, Path(root).name)
    res["matches"] = [raw_match_to_dict(m) for m in matches]
    res.update(extra)
    out = tmp_path / name
    write_results(out, res)
    return out


def cand(url="https://github.com/o/r", kind="repository", backend="scanoss", licenses=None, **kw):
    return Candidate(origin_url=url, kind=kind, backend=backend,
                     licenses=licenses if licenses is not None else
                     [{"spdx": "MIT", "status": "declared", "evidence": "scanoss_component_declared",
                       "confidence": "medium"}], **kw)


def build(root, results=(), **kw):
    kw.setdefault("use_scancode", False)
    return report.build_report(Path(root), [Path(r) for r in results], **kw)


def text_of(rep):
    return json.dumps(rep, ensure_ascii=False).lower()


C1 = CASES / "C1" / "C1_so_2019_cc4.js"


# ---- the acceptance cases ------------------------------------------------------------------

def test_c3_attribution_present_not_flagged(tmp_path):
    root = project(tmp_path, {"src/C3_so_with_attribution.js": CASES / "C3" / "C3_so_with_attribution.js"})
    rep = build(root, [se_results(tmp_path, root)])
    (f,) = rep["findings"]
    assert f["attribution"]["status"] == "present"
    assert f["attribution"]["evidence"] and f["attribution"]["evidence"][0]["line"] == 1
    assert f["flag"] is False and f["flag_reasons"] == []
    assert f["candidates"][0]["origin_url"] == "https://stackoverflow.com/a/58437909"


def test_c4_header_present_license_mit_not_flagged(tmp_path):
    root = project(tmp_path, {"src/C4_attrs_filters_MIT.py": CASES / "C4" / "C4_attrs_filters_MIT.py"})
    rep = build(root, [scanoss_results(tmp_path, root)])
    (f,) = rep["findings"]
    assert f["match_type"] == "file"
    assert f["attribution"]["status"] == "present"
    assert any(e["spdx"] == "MIT" and e["source"] == "file_header" for e in f["local_license_evidence"])
    assert f["flag"] is False
    assert f["candidates"][0]["licenses"][0]["spdx"] == "MIT"


def test_c5_gpl_in_mit_project_flagged_with_conflict_note(tmp_path):
    root = project(tmp_path, {"LICENSE": CASES / "C5" / "LICENSE",
                              "src/C5_ansible_unfrackpath_GPL3.py": CASES / "C5" / "C5_ansible_unfrackpath_GPL3.py"})
    rep = build(root, [scanoss_results(tmp_path, root)])
    (f,) = rep["findings"]
    assert f["flag"] is True
    assert {"unattributed", "license_conflict"} <= set(f["flag_reasons"])
    assert f["attribution"]["status"] == "absent"
    assert f["candidates"][0]["origin_url"].startswith("https://github.com/ansible/ansible")
    assert any(l["spdx"].startswith("GPL-3.0") for l in f["candidates"][0]["licenses"])
    assert any("MIT" in (ev["spdx"] or "") for ev in rep["project_license"])
    assert "copyleft" in f["obligation"] and "not a legal opinion" in f["obligation"]
    out = text_of(rep)
    for bad in FORBIDDEN:
        assert bad not in out, bad


def test_c6_vendored_not_a_finding(tmp_path):
    root = project(tmp_path, {"app.js": CASES / "C8" / "C8_original.py"})
    shutil.copytree(CASES / "C6", root, dirs_exist_ok=True)
    rep = build(root, [cd_results(tmp_path, root)])
    assert rep["findings"] == []
    assert rep["coverage"]["skipped_files"]["vendored"]["count"] >= 1
    (dep,) = [d for d in rep["dependencies"] if d["name"] == "is-odd"]
    assert dep["version"] == "3.0.1" and dep["license"]["spdx"] == "MIT"
    assert dep["license"]["evidence"] == "clearlydefined"
    assert "strict" in rep["tiers_run"]


def test_c9_no_license_all_rights_reserved(tmp_path):
    root = project(tmp_path, {"src/C9_nolicense_noamopt.py": CASES / "C9" / "C9_nolicense_noamopt.py"})
    rep = build(root, [scanoss_results(tmp_path, root)])
    (f,) = rep["findings"]
    assert "all rights are reserved" in f["obligation"]
    assert "no_license_all_rights_reserved" in f["flag_reasons"] and f["flag"] is True
    assert f["attribution"]["status"] == "other_reference"      # the docstring cites a web page, not this source
    assert "public domain" not in f["obligation"].lower()


def test_c2_multiple_candidates_no_direction(tmp_path):
    root = project(tmp_path, {"src/C2_so_2011_cc3.js": CASES / "C2" / "C2_so_2011_cc3.js"})
    sb_res = scanoss_results(tmp_path, root)
    se_res = se_results(tmp_path, root, search=lambda q, p: [7220510] if "syntaxHighlight" in q else [])
    rep = build(root, [sb_res, se_res])
    assert len(rep["findings"]) == 1
    f = rep["findings"][0]
    urls = {c["origin_url"] for c in f["candidates"]}
    assert urls == {"https://github.com/cloudykangaroo/cloudykangaroo", "https://stackoverflow.com/a/7220510"}
    assert any("similarity cannot establish who copied from whom" in n for n in f["notes"])
    assert not any(k in rep or k in f for k in ("origin", "source", "original"))
    assert set(rep["tiers_run"]) >= {"strict-local", "fingerprint", "open"}


def test_obligation_so_template(tmp_path):
    root = project(tmp_path, {"src/C1_so_2019_cc4.js": C1})
    rep = build(root, [se_results(tmp_path, root)])
    (f,) = rep["findings"]
    ob = f["obligation"]
    assert "If copyrightable" in ob and "https://stackoverflow.com/a/58437909" in ob
    assert "Scotty Jamison" in ob and "CC-BY-SA-4.0" in ob and "ShareAlike" in ob
    assert f["match_type"] == "exact" and f["flag"] is True and f["flag_reasons"] == ["unattributed"]
    assert f["candidates"][0]["licenses"][0]["evidence"] == "so_answer_current"
    assert f["lines"][0] <= f["lines"][1] and f["matched_lines"]
    assert "async function doAsyncTask" in "\n".join(f["matched_lines"])


def test_no_findings_never_clean(tmp_path):
    root = project(tmp_path, {"src/C8_original.py": CASES / "C8" / "C8_original.py"})
    rep = build(root, [se_results(tmp_path, root, search=lambda q, p: [])])
    assert rep["findings"] == []
    assert rep["summary"].startswith("No matches found in the checked scope")
    assert rep["coverage"]["not_checked"]
    out = text_of(rep)
    for bad in FORBIDDEN:
        assert bad not in out, bad


def test_strict_only_coverage_suggests_other_tiers(tmp_path):
    root = project(tmp_path, {"src/C8_original.py": CASES / "C8" / "C8_original.py"})
    rep = build(root)
    assert rep["tiers_run"] == ["strict-local"]
    nc = " ".join(rep["coverage"]["not_checked"]).lower()
    assert "fingerprint" in nc and "open tier" in nc
    notes = " ".join(rep["coverage"]["notes"]).lower()
    assert "fingerprint" in notes and "open" in notes and "approve" in notes
    assert rep["coverage"]["services_contacted"] == [] and rep["coverage"]["requests"] == []


def test_backend_errors_surface(tmp_path):
    root = project(tmp_path, {"src/C1_so_2019_cc4.js": C1})
    rep = build(root, [se_results(tmp_path, root, quota=0, search=hit_c1)])
    cov = rep["coverage"]
    assert any(e["code"] == "quota_exhausted" and e["backend"] == "stackexchange" for e in cov["errors"])
    assert cov["not_sent"] and all(n["backend"] == "stackexchange" for n in cov["not_sent"])
    assert "not checked" in rep["summary"].lower() or "unchecked" in rep["summary"].lower()
    assert not rep["summary"].startswith("No matches found in the checked scope.") or "not" in rep["summary"].lower()
    assert any("incomplete" in n.lower() for n in cov["not_checked"])


def test_services_and_requests_disclosed(tmp_path):
    root = project(tmp_path, {"src/C1_so_2019_cc4.js": C1})
    rep = build(root, [se_results(tmp_path, root), scanoss_results(tmp_path, root)])
    cov = rep["coverage"]
    assert set(cov["services_contacted"]) == {"api.stackexchange.com", "api.osskb.org"}
    assert cov["requests"] and all(set(r) >= {"backend", "method", "url", "status"} for r in cov["requests"])
    assert {r["backend"] for r in cov["requests"]} == {"stackexchange", "scanoss"}
    assert cov["kb_version"] == "26.10.19"
    assert any("2022" in x for x in cov["not_checked"])             # SCANOSS Stack Overflow snapshot caveat


def test_scancode_not_installed_note(tmp_path, monkeypatch):
    import headers
    monkeypatch.setattr(headers, "find_exe", lambda *a, **k: None)
    root = project(tmp_path, {"src/C4_attrs_filters_MIT.py": CASES / "C4" / "C4_attrs_filters_MIT.py"})
    rep = build(root, use_scancode=True)
    assert rep["coverage"]["scancode"] == "not_installed"
    assert any("ScanCode" in n and "built-in" in n for n in rep["coverage"]["notes"])
    assert build(root, use_scancode=False)["coverage"]["scancode"] == "disabled"


def test_scancode_used_when_available(tmp_path, monkeypatch):
    import headers
    monkeypatch.setattr(headers, "find_exe", lambda *a, **k: "/bin/scancode")
    sample = json.loads((RESPONSES / "scancode_sample.json").read_text(encoding="utf-8"))

    def runner(cmd, **kw):
        out = Path(cmd[cmd.index("--json") + 1])
        out.write_text(json.dumps(sample), encoding="utf-8")

        class R:
            returncode = 0
        return R()

    root = project(tmp_path, {"src/C4_attrs_filters_MIT.py": CASES / "C4" / "C4_attrs_filters_MIT.py"})
    rep = build(root, use_scancode=True, scancode_runner=runner)
    assert rep["coverage"]["scancode"] == "used"


def test_results_root_mismatch_error(tmp_path, capsys):
    root = project(tmp_path, {"src/C1_so_2019_cc4.js": C1})
    res = se_results(tmp_path, root)
    other = tmp_path / "other"
    shutil.copytree(root, other)
    with pytest.raises(report.ReportError) as ei:
        build(other, [res])
    assert ei.value.code == "results_root_mismatch"
    rc = report.main([str(other), "--results", str(res), "--no-scancode"])
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "results_root_mismatch"


def test_no_source_files_exit_2(tmp_path, capsys):
    root = tmp_path / "docs_only"
    root.mkdir()
    (root / "README.md").write_text("# hi\n")
    (root / "logo.png").write_bytes(b"\x89PNG\0\0")
    rc = report.main([str(root), "--no-scancode"])
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "no_source_files"


def test_everything_vendored_is_no_source_files(tmp_path, capsys):
    shutil.copytree(CASES / "C6", tmp_path / "v")
    assert report.main([str(tmp_path / "v"), "--no-scancode"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "no_source_files"


def test_report_makes_no_network_calls(tmp_path):
    src = (Path(report.__file__)).read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:import|from)\s+([\w.]+)", src, re.M)
    assert not {i.split(".")[0] for i in imports} & {"net", "urllib", "socket", "http", "ssl", "requests", "approval",
                                                      "replay", "ftplib", "smtplib"}
    root = project(tmp_path, {"src/C1_so_2019_cc4.js": C1})
    assert build(root, [se_results(tmp_path, root)])["findings"]       # sockets are blocked by conftest


def test_references_in_comments_listed(tmp_path):
    root = project(tmp_path, {"src/C3_so_with_attribution.js": CASES / "C3" / "C3_so_with_attribution.js",
                              "src/C8_original.py": CASES / "C8" / "C8_original.py"})
    rep = build(root)
    refs = rep["references_in_comments"]
    assert {"file": "src/C3_so_with_attribution.js", "line": 1, "url": "https://stackoverflow.com/a/58437909",
            "kind": "stackoverflow"} in refs
    assert all(set(r) == {"file", "line", "url", "kind"} for r in refs)


# ---- merging, attribution and flags (hand-made results) ---------------------------------------------

def test_overlapping_matches_merge_into_one_finding_with_all_candidates(tmp_path):
    root = project(tmp_path, {"src/a.js": "".join(f"line {i}\n" for i in range(1, 41))})
    r = hand_results(tmp_path, root, [
        RawMatch("src/a.js", 5, 20, "near", cand("https://github.com/a/one")),
        RawMatch("src/a.js", 15, 30, "exact", cand("https://github.com/b/two")),
        RawMatch("src/a.js", 35, 40, "near", cand("https://github.com/c/three")),
    ])
    rep = build(root, [r])
    assert len(rep["findings"]) == 2
    f = rep["findings"][0]
    assert f["lines"] == [5, 30] and f["match_type"] == "exact"
    assert {c["origin_url"] for c in f["candidates"]} == {"https://github.com/a/one", "https://github.com/b/two"}
    assert [x["id"] for x in rep["findings"]] == ["F1", "F2"]


def test_same_origin_from_two_backends_is_one_candidate_with_union_of_licences(tmp_path):
    root = project(tmp_path, {"src/C1_so_2019_cc4.js": C1})
    rep = build(root, [scanoss_results(tmp_path, root), se_results(tmp_path, root)])
    (f,) = rep["findings"]
    (c,) = f["candidates"]
    assert c["origin_url"] == "https://stackoverflow.com/a/58437909"
    assert set(c["backends"]) == {"scanoss", "stackexchange"}
    assert {l["evidence"] for l in c["licenses"]} == {"scanoss_component_declared", "so_answer_current"}


def test_trivial_matched_block_is_dropped(tmp_path):
    root = project(tmp_path, {"src/C7_boilerplate.java": CASES / "C7" / "C7_boilerplate.java"})
    n = len((root / "src" / "C7_boilerplate.java").read_text(encoding="utf-8").splitlines())
    r = hand_results(tmp_path, root, [RawMatch("src/C7_boilerplate.java", 1, n, "near", cand())])
    rep = build(root, [r])
    assert rep["findings"] == []
    assert rep["dropped"] and rep["dropped"][0]["reason"] in ("boilerplate", "too_short", "license_header")


def test_match_outside_eligible_files_is_dropped(tmp_path):
    root = project(tmp_path, {"src/a.js": "x\n" * 30, "node_modules/lib/b.js": "y\n" * 30})
    r = hand_results(tmp_path, root, [RawMatch("node_modules/lib/b.js", 1, 30, "file", cand())])
    rep = build(root, [r])
    assert rep["findings"] == []
    assert rep["dropped"][0]["reason"] in ("declared_dependency", "not_eligible")


def test_file_header_beats_repo_license_for_conflict(tmp_path):
    body = "# SPDX-License-Identifier: GPL-3.0-or-later\n" + "".join(f"x{i} = {i}\n" for i in range(1, 30))
    root = project(tmp_path, {"LICENSE": CASES / "C5" / "LICENSE", "src/g.py": body})
    gpl = cand("https://github.com/o/gpl", licenses=[{"spdx": "GPL-3.0", "status": "declared",
                                                      "evidence": "scanoss_component_declared", "confidence": "medium"}])
    rep = build(root, [hand_results(tmp_path, root, [RawMatch("src/g.py", 2, 30, "near", gpl)])])
    (f,) = rep["findings"]
    assert "license_conflict" not in f["flag_reasons"]          # the file's own header says GPL, matching the candidate
    assert f["attribution"]["status"] == "other_reference"      # ...but its own header is not attribution


def test_other_reference_when_file_cites_something_else(tmp_path):
    body = "# see https://example.org/page for background\n" + "".join(f"x{i} = {i}\n" for i in range(1, 30))
    root = project(tmp_path, {"src/g.py": body})
    rep = build(root, [hand_results(tmp_path, root, [RawMatch("src/g.py", 2, 30, "near", cand())])])
    assert rep["findings"][0]["attribution"]["status"] == "other_reference"
    assert "unattributed" in rep["findings"][0]["flag_reasons"]


def test_github_reference_counts_as_attribution(tmp_path):
    body = "# from https://github.com/Owner/Repo/blob/main/x.py\n" + "".join(f"x{i} = {i}\n" for i in range(1, 30))
    root = project(tmp_path, {"src/g.py": body})
    rep = build(root, [hand_results(tmp_path, root, [RawMatch("src/g.py", 2, 30, "near",
                                                              cand("https://github.com/owner/repo"))])])
    assert rep["findings"][0]["attribution"]["status"] == "present"


def test_unknown_license_gets_unknown_obligation(tmp_path):
    root = project(tmp_path, {"src/g.py": "".join(f"x{i} = {i}\n" for i in range(1, 30))})
    c = cand("https://github.com/o/u", licenses=[{"spdx": None, "status": "unknown", "evidence": "clearlydefined",
                                                  "confidence": "low"}])
    rep = build(root, [hand_results(tmp_path, root, [RawMatch("src/g.py", 2, 30, "near", c)])])
    assert "Licence unknown" in rep["findings"][0]["obligation"]


def test_repository_candidate_gets_direction_note(tmp_path):
    root = project(tmp_path, {"src/g.py": "".join(f"x{i} = {i}\n" for i in range(1, 30))})
    rep = build(root, [hand_results(tmp_path, root, [RawMatch("src/g.py", 2, 30, "near", cand())])])
    assert any("who copied from whom" in n for n in rep["findings"][0]["notes"])


def test_matched_lines_are_capped(tmp_path):
    root = project(tmp_path, {"src/g.py": "".join(f"x{i} = {i}\n" for i in range(1, 300))})
    rep = build(root, [hand_results(tmp_path, root, [RawMatch("src/g.py", 1, 280, "file", cand())])])
    f = rep["findings"][0]
    assert len(f["matched_lines"]) <= report.MAX_MATCHED_LINES
    assert any("truncated" in n for n in f["notes"])


def test_dependency_confidence_is_normalised(tmp_path):
    root = project(tmp_path, {"app.js": CASES / "C8" / "C8_original.py"})
    shutil.copytree(CASES / "C6", root, dirs_exist_ok=True)
    rep = build(root, [cd_results(tmp_path, root)])
    assert all(d["license"]["confidence"] in ("high", "medium", "low", None) for d in rep["dependencies"])


def test_skipped_files_are_aggregated_with_examples(tmp_path):
    files = {"src/a.py": "x = 1\n" * 30}
    files.update({f"node_modules/p{i}/index.js": "y\n" for i in range(12)})
    files["src/k.pem"] = "-----BEGIN-----\n"
    root = project(tmp_path, files)
    rep = build(root)
    sk = rep["coverage"]["skipped_files"]
    assert sk["vendored"]["count"] == 12 and len(sk["vendored"]["examples"]) <= 5
    assert sk["sensitive"]["count"] == 1


@pytest.mark.parametrize("payload", ['{"backend": "x"}', "[]", "not json",
                                     '{"backend":"scanoss","plan_sha256":"a","root":"proj","tier":"fingerprint",'
                                     '"matches":[{"rel":"a","start":"x"}]}',
                                     '{"backend":"scanoss","plan_sha256":"a","root":"proj","tier":"bogus"}',
                                     '{"backend":"scanoss","plan_sha256":"a","root":"proj","tier":"fingerprint",'
                                     '"matches":[{"rel":"src/a.py","start":1,"end":5,"match_type":"near",'
                                     '"candidate":{"origin_url":"https://x.org"}}]}'])
def test_malformed_results_are_a_clean_error(tmp_path, payload, capsys):
    root = project(tmp_path, {"src/a.py": "x = 1\n" * 30})
    bad = tmp_path / "bad.json"
    bad.write_text(payload, encoding="utf-8")
    rc = report.main([str(root), "--results", str(bad), "--no-scancode"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["error"] == "results_invalid"


def test_cli_prints_json_report(tmp_path, capsys):
    root = project(tmp_path, {"src/C1_so_2019_cc4.js": C1})
    res = se_results(tmp_path, root)
    rc = report.main([str(root), "--results", str(res), "--no-scancode"])
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["findings"] and rep["disclaimer"] == report.DISCLAIMER
    assert "not a legal opinion" in rep["disclaimer"]
    assert rep["root"] == "proj"


def test_findings_keys(tmp_path):
    root = project(tmp_path, {"src/C1_so_2019_cc4.js": C1})
    f = build(root, [se_results(tmp_path, root)])["findings"][0]
    assert set(f) == {"id", "file", "lines", "matched_lines", "match_type", "candidates", "local_license_evidence",
                      "attribution", "flag", "flag_reasons", "obligation", "notes"}
    assert set(f["attribution"]) == {"status", "evidence"}


def test_url_the_code_uses_is_not_attribution(tmp_path):
    # C1 holds 'https://jsonplaceholder.typicode.com/comments?' as an endpoint, which is not a reference to a source
    root = project(tmp_path, {"src/C1_so_2019_cc4.js": C1})
    rep = build(root, [se_results(tmp_path, root)])
    assert rep["findings"][0]["attribution"]["status"] == "absent"
    assert rep["references_in_comments"] == []


@pytest.mark.parametrize("line, counts", [
    ("# https://example.org/x", True), ("x = 1  # https://example.org/x", True),
    ("// see https://example.org/x", True), ("/* https://example.org/x */", True), (" * https://example.org/x", True),
    ('"""Copied from https://example.org/x', True), ("// https://stackoverflow.com/a/123", True),
    ("// https://github.com/o/r/blob/main/a.py", True),
    ("url = 'https://api.example.org/v1'", False), ("fetch(\"https://api.example.org/v1\").then(go)", False),
])
def test_which_urls_count_as_references(tmp_path, line, counts):
    root = project(tmp_path, {"src/a.py": line + "\n" + "x = 1\n" * 30})
    refs = build(root)["references_in_comments"]
    assert bool(refs) == counts, line


def _snippet_file(header):
    return header + "".join(f"x{i} = {i}\n" for i in range(1, 30))


def test_own_spdx_tag_is_not_attribution_of_a_snippet_from_someone_else(tmp_path):
    root = project(tmp_path, {"src/g.py": _snippet_file("# SPDX-License-Identifier: MIT\n")})
    rep = build(root, [hand_results(tmp_path, root, [RawMatch("src/g.py", 2, 30, "near", cand())])])
    f = rep["findings"][0]
    assert f["attribution"]["status"] != "present"
    assert "unattributed" in f["flag_reasons"]


def test_header_licence_plus_copyright_naming_the_author_is_attribution(tmp_path):
    root = project(tmp_path, {"src/g.py": _snippet_file(
        "# SPDX-License-Identifier: MIT\n# Copyright 2020 Jane Doe\n")})
    c = cand(author="Jane Doe")
    rep = build(root, [hand_results(tmp_path, root, [RawMatch("src/g.py", 3, 31, "near", c)])])
    assert rep["findings"][0]["attribution"]["status"] == "present"


def _blank_root(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["root"] = ""
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def test_results_file_with_empty_root_and_errors_is_not_a_tier_run(tmp_path):
    root = project(tmp_path, {"src/g.py": _snippet_file("")})
    res = hand_results(tmp_path, root, [], backend="scanoss", tier="fingerprint", name="early.json",
                       errors=[{"code": "plan_hash_mismatch", "detail": "x"}])
    _blank_root(res)
    rep = build(root, [res])
    assert "fingerprint" not in rep["tiers_run"]
    assert any("fingerprint" in n for n in rep["coverage"]["not_checked"])
    assert any(e["code"] == "plan_hash_mismatch" for e in rep["coverage"]["errors"]) or \
        any(e.get("code") == "plan_hash_mismatch" for e in rep.get("errors", []))


def test_matches_from_a_rootless_results_file_are_refused(tmp_path):
    root = project(tmp_path, {"src/g.py": _snippet_file("")})
    res = hand_results(tmp_path, root, [RawMatch("src/g.py", 2, 30, "near", cand())], name="early2.json",
                       errors=[{"code": "boom", "detail": "x"}])
    _blank_root(res)
    with pytest.raises(report.ReportError) as ei:
        build(root, [res])
    assert ei.value.code == "results_root_mismatch"
