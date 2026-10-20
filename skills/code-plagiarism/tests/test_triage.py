import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

from inventory import SourceFile, read_lines  # noqa: E402
from model import (Candidate, RawMatch, load_results, new_results,  # noqa: E402
                   raw_match_from_dict, raw_match_to_dict, write_results)
from normalize import LocalMatch  # noqa: E402
from triage import (MAX_BLOCK_LINES, Block, declared_tree_reason,  # noqa: E402
                    local_match_drop_reason, merge_adjacent, scanoss_drop_reason,
                    significant_lines, split_blocks, trivial_reason)

CASES = SKILL / "tests" / "fixtures" / "cases"
RESP = SKILL / "tests" / "fixtures" / "responses"


def sf(rel="a.py", lang="hash"):
    return SourceFile(rel=rel, path=Path(rel), lang=lang, size=0)


def blocks_of(path: Path, lang: str):
    lines = read_lines(path)
    return lines, split_blocks(sf(path.name, lang), lines)


def scanoss_match_for(path: Path) -> dict:
    data = json.loads((RESP / "scanoss_by_md5.json").read_text(encoding="utf-8"))
    return data[hashlib.md5(path.read_bytes()).hexdigest()][0]


def cand(url="https://example.org/a", **kw):
    base = dict(origin_url=url, kind="repository", backend="scanoss", author=None,
                author_url=None, date=None, revision=None, licenses=[], matched_pct=None,
                oss_lines=None, purl=[], post_id=None, note=None)
    base.update(kw)
    return Candidate(**base)


def test_split_blocks_top_level_defs():
    lines = ["def a(x):", "    return x + 1", "",
             "def b(x):", "    return x + 2", "",
             "def c(x):", "    return x + 3"]
    blocks = split_blocks(sf(), lines)
    assert [(b.start, b.end) for b in blocks] == [(1, 2), (4, 5), (7, 8)]
    assert blocks[1].lines == lines[3:5]
    assert blocks[0].lang == "hash" and blocks[0].rel == "a.py"


def test_split_after_two_blank_lines():
    lines = ["x = 1", "y = 2", "", "", "z = 3"]
    assert [(b.start, b.end) for b in split_blocks(sf(), lines)] == [(1, 2), (5, 5)]


def test_decorator_stays_with_its_def():
    lines = ["@cache", "def a():", "    return 1"]
    assert [(b.start, b.end) for b in split_blocks(sf(), lines)] == [(1, 3)]


def test_long_block_split():
    lines = ["def big():"] + [f"    x{i} = {i}" for i in range(129)]
    assert len(lines) == 130
    blocks = split_blocks(sf(), lines)
    assert len(blocks) >= 3
    assert all(b.end - b.start + 1 <= MAX_BLOCK_LINES for b in blocks)
    covered = [n for b in blocks for n in range(b.start, b.end + 1)]
    assert covered == list(range(1, 131))
    assert all(b.lines == lines[b.start - 1:b.end] for b in blocks)


def test_unknown_language_is_skipped_not_crash():
    assert split_blocks(sf("a.xyz", "cobol"), ["x = 1"] * 10) == []
    blk = Block("a.xyz", 1, 10, "cobol", ["x = 1"] * 10)
    trivial_reason(blk)  # must not raise


def test_empty_file_has_no_blocks():
    assert split_blocks(sf(), []) == []
    assert split_blocks(sf(), ["", "", ""]) == []


def test_split_blocks_time_budget():
    many = [f"x{i} = {i}" for i in range(100_000)]
    t = time.perf_counter()
    blocks = split_blocks(sf(), many)
    assert time.perf_counter() - t < 2
    assert sum(b.end - b.start + 1 for b in blocks) == 100_000
    t = time.perf_counter()
    one = split_blocks(sf("m.js", "c_like"), ["const x = " + "a" * 1_000_000 + ";"])
    assert time.perf_counter() - t < 2
    assert len(one) == 1
    t = time.perf_counter()
    trivial_reason(one[0])
    significant_lines(one[0])
    assert time.perf_counter() - t < 2


def test_significant_lines_excludes_noise():
    blk = Block("a.java", 1, 9, "c_like", [
        "package a;", "import java.util.List;", "", "// comment", "/* c */",
        "class A {", "    int x = 1;", "}", "    {"])
    assert significant_lines(blk) == 2


def test_c7_boilerplate_trivial():
    for name, lang in (("C7_boilerplate.java", "c_like"), ("C7_boilerplate.py", "hash")):
        lines, blocks = blocks_of(CASES / "C7" / name, lang)
        assert blocks
        reasons = {b.start: trivial_reason(b) for b in blocks}
        assert all(r is not None for r in reasons.values()), (name, reasons)
        big = max(blocks, key=lambda b: b.end - b.start)
        assert trivial_reason(big) == "boilerplate", name


def test_short_block_trivial():
    blk = Block("a.py", 1, 5, "hash", ["a = compute(1)", "b = compute(2)", "",
                                        "c = compute(a, b)", "print(c)"])
    assert significant_lines(blk) == 4
    assert trivial_reason(blk) == "too_short"


def test_license_header_block_trivial():
    lines = ["// SPDX-License-Identifier: MIT", "// Copyright (c) 2020 Acme Corp",
             "//", "// Permission is hereby granted, free of charge, to any person",
             "// obtaining a copy of this software", "", "var a = 1;", "var b = 2;"]
    blk = Block("a.js", 1, len(lines), "c_like", lines)
    assert trivial_reason(blk) == "license_header"


def test_c1_block_not_trivial():
    lines, blocks = blocks_of(next((CASES / "C1").glob("*.js")), "c_like")
    fn = [b for b in blocks if any("async function" in ln for ln in b.lines)]
    assert len(fn) == 1
    assert trivial_reason(fn[0]) is None


def test_real_code_with_header_comment_is_not_license_header():
    lines = ["# Copyright (c) 2020 Acme"] + [f"value_{i} = compute({i}) + other({i})" for i in range(10)]
    blk = Block("a.py", 1, len(lines), "hash", lines)
    assert trivial_reason(blk) is None


def test_accessor_regex_does_not_match_calls_or_lookalikes():
    lines = ["def settle(self):", "    return self.x", "def issue(self):", "    return 1",
             "def island(self):", "    return 2", "def getaway(self):", "    return 3",
             "def total(self):", "    return 4", "def other(self):", "    return 5"]
    blk = Block("a.py", 1, len(lines), "hash", lines)
    assert trivial_reason(blk) is None


def test_scanoss_threshold():
    c7 = scanoss_match_for(CASES / "C7" / "C7_boilerplate.py")
    assert (c7["matched"], c7["lines"]) == ("25%", "14-20")
    assert scanoss_drop_reason(c7) == "below_threshold"
    c5b = scanoss_match_for(CASES / "C5b" / "C5b_git_strbuf_commented_addf_GPL2.c")
    assert scanoss_drop_reason(c5b) is None
    c4 = scanoss_match_for(CASES / "C4" / "C4_attrs_filters_MIT.py")
    assert c4["id"] == "file"
    assert scanoss_drop_reason(c4) is None


def test_scanoss_threshold_edges():
    assert scanoss_drop_reason({"id": "snippet", "matched": "50%", "lines": "1-8"}) is None
    assert scanoss_drop_reason({"id": "snippet", "matched": "49%", "lines": "1-20"}) == "below_threshold"
    assert scanoss_drop_reason({"id": "snippet", "matched": "90%", "lines": "1-7"}) == "below_threshold"
    assert scanoss_drop_reason({"id": "snippet", "matched": "90%", "lines": "all"}) is None
    assert scanoss_drop_reason({"id": "snippet", "matched": "junk", "lines": "x"}) is None
    assert scanoss_drop_reason({"id": "file"}) is None


def test_vendored_path_is_declared_dependency():
    assert declared_tree_reason("node_modules/left-pad/index.js") == "declared_dependency"
    assert declared_tree_reason("src/vendor/x.py") == "declared_dependency"
    assert declared_tree_reason("src\\vendor\\x.py") == "declared_dependency"
    assert declared_tree_reason("src/app.py") is None
    assert declared_tree_reason("vendor") is None  # a file named like a vendor dir


def test_local_match_drop_reason():
    def lm(t, tokens, cov):
        return LocalMatch(t, 1, 5, 1, 5, tokens, cov)
    assert local_match_drop_reason(lm("near", 80, 0.6), 200) == "weak_near"
    assert local_match_drop_reason(lm("near", 120, 0.6), 200) is None
    assert local_match_drop_reason(lm("near", 80, 0.8), 200) is None
    assert local_match_drop_reason(lm("exact", 60, 0.95), 70) is None
    assert local_match_drop_reason(lm("normalised", 60, 0.95), 70) is None


def test_merge_adjacent_same_origin_only():
    a = RawMatch("f.py", 3, 10, "exact", cand("https://x/1"))
    b = RawMatch("f.py", 12, 20, "exact", cand("https://x/1"))
    out = merge_adjacent([b, a])
    assert [(m.start, m.end) for m in out] == [(3, 20)]
    c = RawMatch("f.py", 12, 20, "exact", cand("https://x/2"))
    out = merge_adjacent([a, c])
    assert [(m.start, m.end, m.candidate.origin_url) for m in out] == [
        (3, 10, "https://x/1"), (12, 20, "https://x/2")]
    far = RawMatch("f.py", 30, 40, "exact", cand("https://x/1"))
    assert len(merge_adjacent([a, far])) == 2
    other_file = RawMatch("g.py", 12, 20, "exact", cand("https://x/1"))
    assert len(merge_adjacent([a, other_file])) == 2


def test_merge_adjacent_weakest_type_and_inputs_untouched():
    a = RawMatch("f.py", 3, 10, "exact", cand(matched_pct=90.0, oss_lines="1-8"))
    b = RawMatch("f.py", 11, 20, "near", cand(matched_pct=60.0, oss_lines="9-18"))
    out = merge_adjacent([a, b])
    assert len(out) == 1 and out[0].match_type == "near"
    assert out[0].candidate.matched_pct == 60.0
    assert (a.start, a.end, a.match_type) == (3, 10, "exact")
    assert merge_adjacent([]) == []


def test_raw_match_roundtrip():
    c = cand("https://stackoverflow.com/a/1", kind="stackoverflow", backend="stackexchange",
             author="Ann", author_url="https://stackoverflow.com/users/1", date="2019-10-17",
             revision="17", licenses=[{"spdx": "CC-BY-SA-4.0", "status": "declared",
                                       "evidence": "so_revision", "confidence": "high"}],
             matched_pct=78.0, oss_lines="3-14", purl=["pkg:stackoverflow/1"], post_id=1,
             note="n")
    m = RawMatch("src/a.py", 3, 14, "near", c)
    d = raw_match_to_dict(m)
    json.dumps(d)
    assert d["candidate"]["post_id"] == 1
    assert raw_match_from_dict(d) == m
    sparse = raw_match_from_dict({"rel": "a", "start": 1, "end": 2, "match_type": "file",
                                  "candidate": {"origin_url": "u", "kind": "package",
                                                "backend": "scanoss"}})
    assert sparse.candidate.licenses == [] and sparse.candidate.author is None


def test_results_file_roundtrip(tmp_path):
    r = new_results("scanoss", "fingerprint", "abc123", "/proj")
    assert r == {"backend": "scanoss", "tier": "fingerprint", "plan_sha256": "abc123",
                 "root": "/proj", "requests_sent": [], "not_sent": [], "matches": [],
                 "dropped": [], "dependencies": [], "kb_version": None, "errors": [],
                 "notes": []}
    r["notes"].append("é")
    p = tmp_path / "res.json"
    write_results(p, r)
    assert load_results(p) == r
    assert new_results("a", "b", "c", "d")["errors"] is not new_results("a", "b", "c", "d")["errors"]


@pytest.mark.parametrize("missing", ["backend", "plan_sha256", "root"])
def test_load_results_requires_keys(tmp_path, missing):
    r = new_results("scanoss", "fingerprint", "abc", "/p")
    del r[missing]
    p = tmp_path / "r.json"
    p.write_text(json.dumps(r), encoding="utf-8")
    with pytest.raises(ValueError):
        load_results(p)


def test_load_results_rejects_non_object(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_results(p)
