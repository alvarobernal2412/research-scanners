import sys
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

from inventory import read_lines  # noqa: E402
from normalize import (FULL_COVERAGE, K_GRAM, MAX_GAP_TOKENS, MIN_MATCH_TOKENS,  # noqa: E402
                       Token, compare, normalise, token_line_span, tokenize)

CASES = SKILL / "tests" / "fixtures" / "cases"


def c1():
    return read_lines(next((CASES / "C1").glob("*.js")))


def texts(toks):
    return [t.text for t in toks]


def test_comments_stripped_c_like_and_hash():
    src = ["a = 1; // note", "/* multi", "line */ b = 2;", 'c = "x // y";']
    assert texts(tokenize(src, "c_like")) == [
        "a", "=", "1", ";", "b", "=", "2", ";", "c", "=", '"x // y"', ";"]
    assert tokenize(src, "c_like")[4].line == 3
    py = ["a = 1  # c", 'b = "# keep"', "c = '''x", "# not a comment", "'''", "d = 2"]
    out = tokenize(py, "hash")
    assert texts(out) == ["a", "=", "1", "b", "=", '"# keep"', "c", "=",
                          "'''x\n# not a comment\n'''", "d", "=", "2"]
    assert out[-1].line == 6


def test_string_literal_single_token_line_preserved():
    toks = tokenize(["x = 1;", "", "  y = 'a b  c';", "`t", "u` + 1"], "c_like")
    s = [t for t in toks if t.kind == "string"]
    assert [t.text for t in s] == ["'a b  c'", "`t\nu`"]
    assert [t.line for t in s] == [3, 4]
    assert toks[-1].line == 5
    kinds = [(t.text, t.kind) for t in tokenize(["if x1 > 42:"], "hash")]
    assert kinds == [("if", "keyword"), ("x1", "ident"), (">", "op"),
                     ("42", "number"), (":", "op")]


def test_normalise_renamed():
    toks = tokenize(["let a = 'q' + 3;"], "c_like")
    assert normalise(toks, False) == ["let", "a", "=", "'q'", "+", "3", ";"]
    assert normalise(toks, True) == ["let", "$I", "=", "$S", "+", "$N", ";"]


def test_identical_after_reformat_is_exact():
    a = tokenize(c1(), "c_like")
    lines = ["    // extra comment"] + [("\t" + l.strip() + "  ") for l in c1()] + ["/* tail */"]
    b = tokenize(lines, "c_like")
    m = compare(a, b)
    assert m is not None and m.match_type == "exact"
    assert m.coverage >= FULL_COVERAGE
    crlf = "\r\n".join(c1()).split("\r\n")
    assert compare(a, tokenize(crlf, "c_like")).match_type == "exact"


def test_renamed_copy_is_normalised():
    a = tokenize(c1(), "c_like")
    b = tokenize(read_lines(next((CASES / "C10").glob("*.js"))), "c_like")
    m = compare(a, b)
    assert m is not None and m.match_type == "normalised"
    first, last = token_line_span(a, m.a_start, m.a_end)
    assert first <= 5 and last >= 16  # function body (comment lines excluded)


def test_short_overlap_below_threshold():
    shared = ["function f(a) {", "  return a + 1;", "}"]
    a = tokenize(c1() + shared, "c_like")
    b = tokenize(shared + ["let zz = 99;", "zz.go(7);"], "c_like")
    assert compare(a, b) is None


def test_compare_empty_and_tiny():
    t = tokenize(["a = 1;"], "c_like")
    assert compare([], []) is None
    assert compare(t, []) is None
    assert compare([], t) is None
    assert compare(t, t) is None


def test_token_line_span():
    toks = [Token("a", "ident", 3), Token("b", "ident", 4), Token("c", "ident", 9)]
    assert token_line_span(toks, 0, 3) == (3, 9)
    assert token_line_span(toks, 1, 2) == (4, 4)
    assert token_line_span(toks, 0, 0) == (0, 0)


def _timed(fn):
    t0 = time.perf_counter()
    r = fn()
    dt = time.perf_counter() - t0
    print(f"elapsed {dt:.2f}s")
    assert dt < 2.0, dt
    return r


def test_perf_1mb_single_line():
    line = "foo(bar, 12) + baz[3]; " * 45000
    toks = _timed(lambda: tokenize([line], "c_like"))
    assert len(toks) > 100000


def test_perf_200k_lines():
    toks = _timed(lambda: tokenize(["x = 1"] * 200_000, "hash"))
    assert len(toks) == 600_000 and toks[-1].line == 200_000


def test_perf_compare_50k_tokens():
    a = tokenize(filler(6000, "v", 31), "c_like")
    b = tokenize(filler(6000, "v", 31), "c_like")
    assert len(a) >= 50_000
    m = _timed(lambda: compare(a, b))
    assert m is not None
    rep = tokenize(["x = 1;"] * 10000, "c_like")
    _timed(lambda: compare(rep, rep))


def test_perf_unterminated_string_and_comment():
    for src in ('"' + "a" * 1_000_000, "'" * 1 + "\'" * 500_000, "`" + "a" * 1_000_000,
                "x /*" + "a " * 500_000, "'a " * 300_000, '"""' + "b" * 1_000_000):
        _timed(lambda s=src: tokenize([s], "c_like"))
        _timed(lambda s=src: tokenize([s], "hash"))
    assert tokenize(['x = "abc'], "c_like")[-1].line == 1


def test_perf_nested_brackets():
    toks = _timed(lambda: tokenize(["(" * 100_000 + ")" * 100_000], "c_like"))
    assert len(toks) == 200_000
    _timed(lambda: compare(toks, toks))


# ---------------------------------------------------------------- round 1 fixes
import random  # noqa: E402

from normalize import (MAX_DIAG_DRIFT, MIN_DISTINCT_KGRAMS, MIN_KGRAM_VARIETY,  # noqa: E402
                       MIN_MATCH_LINES, NEAR_MIN_COVERAGE, compare_all)


def toks_js(lines):
    return tokenize(lines, "c_like")


def filler(n, tag, seed=1):
    """Unrelated, high-variety lines: random mixes of operators, keywords and names."""
    rnd = random.Random(seed)
    vocab = ["if", "(", ")", "{", "}", "+", "-", "*", "return", "for", "while", "=", ";", ".",
             "[", "]", ",", "<", ">", "&&", "new", "!", "?", ":"]
    out = []
    for i in range(n):
        parts = []
        for _ in range(rnd.randrange(6, 13)):
            r = rnd.random()
            parts.append(rnd.choice(vocab) if r < 0.6 else
                         f"{tag}{rnd.randrange(10**6)}" if r < 0.85 else str(rnd.randrange(999)))
        out.append(" ".join(parts))
    return out


def lines_of(toks, m):
    return (token_line_span(toks, m.a_start, m.a_end),)


def check_mirror(a, b):
    m1, m2 = compare(a, b), compare(b, a)
    assert m1 is not None and m2 is not None
    assert (m1.match_type, m1.coverage, m1.tokens) == (m2.match_type, m2.coverage, m2.tokens)
    assert (m1.a_start, m1.a_end, m1.b_start, m1.b_end) == (m2.b_start, m2.b_end, m2.a_start, m2.a_end)
    return m1


def test_constants_ruling():
    # ruling: MAX_GAP_TOKENS raised from the plan's 8 to 40 (symmetric, both sides)
    assert (K_GRAM, MAX_GAP_TOKENS, MIN_MATCH_TOKENS, MIN_MATCH_LINES) == (10, 40, 40, 5)
    assert (FULL_COVERAGE, NEAR_MIN_COVERAGE, MAX_DIAG_DRIFT) == (0.9, 0.5, 64)
    assert (MIN_KGRAM_VARIETY, MIN_DISTINCT_KGRAMS) == (0.5, 20)


def test_insertions_symmetric_near():
    orig = c1()
    a = toks_js(orig)
    cases = {
        "2x n++": ["  n++;"] * 2,
        "2 statements": [f"  q{i} = w{i} + {i};" for i in range(2)],
        "12x n++": ["  n++;"] * 12,
    }
    for name, ins in cases.items():
        b = toks_js(orig[:10] + ins + orig[10:])
        m = check_mirror(a, b)
        assert m.match_type == "near", name
        assert NEAR_MIN_COVERAGE <= m.coverage < 1.0, name
        # b span is longer than a span by exactly the inserted tokens
        assert (m.b_end - m.b_start) - (m.a_end - m.a_start) == len(toks_js(ins)), name
        assert m.tokens == m.b_end - m.b_start, name


def test_k1_few_tokens_not_inflated():
    names = [f"    'n{i}', 'm{i}', 'k{i}'," for i in range(20)]
    a = tokenize(["NAMES = ["] + names + ["]"], "hash")
    b = tokenize(["COLORS = ['red', 'green', 'blue', 'cyan', 'pink', 'gold']"], "hash")
    assert compare(a, b) is None and compare(b, a) is None


def test_all_lists_are_boilerplate():
    a = tokenize(["__all__ = ["] + [f"    'Alpha{i}'," for i in range(80)] + ["]"], "hash")
    b = tokenize(["__all__ = ["] + [f"    'Beta{i}'," for i in range(70)] + ["]"], "hash")
    assert compare(a, b) is None


def bean(cls, fields):
    out = ["package com.example.model;", "", "import java.util.Objects;", "", f"public class {cls} {{"]
    for t, f in fields:
        out.append(f"    private {t} {f};")
    out.append("")
    for t, f in fields:
        F = f[0].upper() + f[1:]
        out += [f"    public {t} get{F}() {{", f"        return {f};", "    }", "",
                f"    public void set{F}({t} {f}) {{", f"        this.{f} = {f};", "    }", ""]
    out.append("}")
    return out


def test_boilerplate_floods_dropped():
    words = ["name", "email", "street", "city", "zip", "phone", "age", "score", "title", "owner",
             "status", "created", "updated", "price", "qty"]
    a = toks_js(bean("Customer", [("String", w + "A") for w in words]))
    b = toks_js(bean("Order", [("String", w[::-1] + "B") for w in words]))
    assert compare(a, b) is None
    big_a = toks_js(bean("Big", [("String", f"fieldA{i}") for i in range(100)]))
    big_b = toks_js(bean("Other", [("Integer", f"q{i}") for i in range(100)]))
    assert compare(big_a, big_b) is None
    t1 = [f"    {{ id: {i}, name: 'item{i}', price: {i * 3}, active: true }}," for i in range(60)]
    t2 = [f"    {{ id: {i + 500}, name: 'p{i}', price: {i * 7}, active: false }}," for i in range(60)]
    assert compare(toks_js(["const A = ["] + t1 + ["];"]), toks_js(["const B = ["] + t2 + ["];"])) is None


def test_real_logic_still_matches_after_filters():
    a = toks_js(c1())
    c10 = toks_js(read_lines(next((CASES / "C10").glob("*.js"))))
    assert compare(a, c10).match_type == "normalised"
    assert compare(a, a).match_type == "exact"


def test_embedded_copy_spans():
    f1, f2 = filler(250, "u", 1), filler(250, "v", 2)
    a = toks_js(c1())
    b = toks_js(f1 + c1() + f2)
    m = compare(a, b)
    assert m.match_type == "exact"
    assert token_line_span(a, m.a_start, m.a_end) == (4, 17)
    assert token_line_span(b, m.b_start, m.b_end) == (254, 267)
    assert (m.b_end - m.b_start) == (m.a_end - m.a_start) == m.tokens
    r = compare(b, a)
    assert r.match_type == "exact" and token_line_span(b, r.a_start, r.a_end) == (254, 267)


def test_two_copies_both_reported():
    f = filler(200, "u", 3)
    a = toks_js(c1())
    b = toks_js(f[:100] + c1() + f[100:] + c1())
    ms = compare_all(a, b)
    assert len(ms) == 2 and all(m.match_type == "exact" for m in ms)
    assert [m.b_start for m in ms] == sorted(m.b_start for m in ms)
    assert token_line_span(b, ms[0].b_start, ms[0].b_end) == (104, 117)
    assert token_line_span(b, ms[1].b_start, ms[1].b_end) == (221, 234)
    assert all(token_line_span(a, m.a_start, m.a_end) == (4, 17) for m in ms)


def test_swapped_functions_two_regions():
    f1 = c1()[3:16]
    f2 = ["function other(x, y) {", "  const z = x * y + 3;", "  if (z > 10) {", "    return compute(z, x);",
          "  }", "  for (let i = 0; i < y; i++) { x += i; }", "  return x - y;", "}"]
    a, b = toks_js(f1 + f2), toks_js(f2 + f1)
    ms = compare_all(a, b)
    assert len(ms) == 2
    assert [m.a_start for m in ms] == sorted(m.a_start for m in ms)
    assert all(m.match_type == "exact" for m in ms)
    spans = [(token_line_span(a, m.a_start, m.a_end), token_line_span(b, m.b_start, m.b_end)) for m in ms]
    assert spans == [((1, 12), (9, 20)), ((14, 21), (1, 8))]
    best = compare(a, b)
    assert best is not None and best.tokens == max(m.tokens for m in ms)


def test_verbatim_copy_followed_by_boilerplate():
    setters = [f"  set{i}(v) {{ this.f{i} = v; }}" for i in range(15)]
    f = filler(40, "u", 4)
    a = toks_js(c1() + setters)
    b = toks_js(setters + f + c1())
    m = compare(a, b)
    assert m.match_type == "exact" and m.coverage >= FULL_COVERAGE
    assert token_line_span(a, m.a_start, m.a_end) == (4, 17)
    assert token_line_span(b, m.b_start, m.b_end) == (59, 72)


def test_paste_into_large_file():
    rnd = random.Random(5)
    x = filler(1500, "x", 6)
    y = filler(400, "y", 7)
    pasted = y[50:350]
    a = toks_js(x[:700] + pasted + x[700:])
    b = toks_js(y)
    m = compare(a, b)
    assert m.match_type == "exact"
    la, lb = token_line_span(a, m.a_start, m.a_end), token_line_span(b, m.b_start, m.b_end)
    assert abs(la[0] - 701) <= 1 and abs(la[1] - 1000) <= 1
    assert abs(lb[0] - 51) <= 1 and abs(lb[1] - 350) <= 1
    assert rnd is not None


def test_large_shared_block_between_big_files():
    shared = filler(1200, "s", 8)
    a = toks_js(filler(1500, "a", 9)[:600] + shared + filler(1500, "a", 10)[:700])
    b = toks_js(filler(1500, "b", 11)[:300] + shared + filler(1500, "b", 12)[:900])
    m = compare(a, b)
    assert m is not None and m.match_type == "exact"
    la, lb = token_line_span(a, m.a_start, m.a_end), token_line_span(b, m.b_start, m.b_end)
    assert abs(la[0] - 601) <= 1 and abs(la[1] - 1800) <= 1
    assert abs(lb[0] - 301) <= 1 and abs(lb[1] - 1500) <= 1


def test_later_exact_copy_beats_earlier_renamed_copy():
    c10 = read_lines(next((CASES / "C10").glob("*.js")))
    fill = [f"var w{i} = [{i}];" for i in range(30)]
    a = toks_js(c1())
    for lead in (c10, c10 * 9):
        b = toks_js(lead + fill + c1())
        m = compare(a, b)
        assert m.match_type == "exact"
        last = b[-1].line
        assert token_line_span(b, m.b_start, m.b_end) == (last - 13, last)


def test_original_code_no_match_same_family():
    fn1 = ["function checksum(buf) {", "  let h = 17;", "  for (let i = 0; i < buf.length; i++) {",
           "    h = (h * 31 + buf[i]) % 65521;", "  }", "  return h ^ (h >>> 3);", "}"]
    fn2 = ["async function loadAll(urls) {", "  const out = new Map();", "  for (const u of urls) {",
           "    const r = await fetch(u);", "    out.set(u, await r.text());", "  }", "  return out;", "}"]
    assert compare(toks_js(fn1), toks_js(fn2)) is None
    assert compare(toks_js(c1()), toks_js(fn2)) is None
    c8 = tokenize(read_lines(next((CASES / "C8").glob("*.py"))), "hash")
    other = tokenize(["def merge_runs(runs, gap=2):", "    runs = sorted(runs)", "    out = [list(runs[0])]",
                      "    for lo, hi in runs[1:]:", "        if lo - out[-1][1] <= gap:",
                      "            out[-1][1] = max(out[-1][1], hi)", "        else:",
                      "            out.append([lo, hi])", "    return [tuple(r) for r in out]"], "hash")
    assert compare(c8, other) is None and compare(other, c8) is None


def test_compare_all_limits_and_empty():
    assert compare_all([], []) == []
    a = toks_js(c1())
    b = toks_js(c1() * 8)
    assert len(compare_all(a, b, max_regions=3)) == 3


def test_matchtype_checked_inside_chain():
    # a near-copy where only a rename differs in one place is not "exact"
    mod = [l.replace("postId", "pid") for l in c1()]
    m = compare(toks_js(c1()), toks_js(mod))
    assert m.match_type in ("normalised", "near")


def test_perf_200k_low_variety_compare():
    xs = tokenize(["x = 1"] * 33_334, "hash")
    assert len(xs) >= 100_000
    _timed(lambda: compare(xs, xs))
    ys = tokenize(["y = 2"] * 33_334, "hash")
    _timed(lambda: compare(xs, ys))
    rnd = random.Random(7)
    sa = tokenize([" ".join(rnd.choice("();") for _ in range(10)) for _ in range(10000)], "c_like")
    sb = tokenize([" ".join(rnd.choice("();") for _ in range(10)) for _ in range(10000)], "c_like")
    _timed(lambda: compare(sa, sb))
    pa = tokenize([" ".join("a b ( ) ; , ."[2 * ((i + j * 10) % 7)] for i in range(10)) for j in range(10000)],
                  "c_like")
    _timed(lambda: compare(pa, pa))


def test_perf_200k_varied_with_shared_block():
    shared = filler(4800, "s", 21)
    a = toks_js(filler(6000, "a", 22)[:5000] + shared + filler(6000, "a", 23)[:5200])
    b = toks_js(filler(6000, "b", 24)[:3000] + shared + filler(6000, "b", 25)[:5200])
    assert len(a) > 120_000
    m = _timed(lambda: compare(a, b))
    assert m is not None and m.match_type == "exact"
    assert abs(token_line_span(a, m.a_start, m.a_end)[0] - 5001) <= 1
    assert abs(token_line_span(b, m.b_start, m.b_end)[0] - 3001) <= 1


def test_threshold_applies_to_both_spans():
    # b is a 5-line, <40-token block; a is the same block with 24 tokens inserted, so
    # a's span reaches 40+ tokens and coverage stays >= 0.5, yet b's span is too short.
    for seed in range(100):
        lines = filler(5, "t", seed)
        b = toks_js(lines)
        if 34 <= len(b) < MIN_MATCH_TOKENS:
            break
    else:
        raise AssertionError("no seed")
    ins = filler(3, "i", 99)
    a = toks_js(lines[:2] + ins + lines[2:])
    assert len(a) - len(b) >= MIN_MATCH_TOKENS - len(b) + 5 and len(a) - len(b) <= MAX_GAP_TOKENS
    assert compare(a, b) is None and compare(b, a) is None


# ---------------------------------------------------------------- round 2 fixes
def turtle_program(tag):
    """Repetitive real-looking program: one drawing routine pasted three times (variety ~0.33)."""
    block = filler(60, "blk", 11)
    out = []
    for k in range(3):
        out += [f"def part_{tag}{k}(t):"] + block
    return out


def test_repetitive_self_copy_is_exact():
    from normalize import _hashes
    a = toks_js(turtle_program("a"))
    assert len(a) > 800
    h = _hashes(normalise(a, True))
    assert len(set(h)) / len(h) < MIN_KGRAM_VARIETY  # repetitive enough to trip the filter
    m = compare(a, a)
    assert m is not None and m.match_type == "exact" and (m.a_start, m.a_end) == (0, len(a))
    host = filler(300, "h", 12)
    b = toks_js(host[:100] + turtle_program("a") + host[100:])
    m = compare(a, b)
    assert m is not None and m.match_type == "exact"
    assert token_line_span(b, m.b_start, m.b_end) == (101, 283)
    assert token_line_span(a, m.a_start, m.a_end) == (1, 183)


def test_variety_still_rejects_non_identical_boilerplate():
    a = tokenize([f"    self.f{i} = kwargs.get('k{i}', {i})" for i in range(40)], "hash")
    b = tokenize([f"    self.g{i} = kwargs.get('q{i}', {i + 7})" for i in range(40)], "hash")
    assert compare(a, b) is None and compare(b, a) is None


def test_variety_required_on_both_sides():
    rep = ["  p = q ( r , 1 ) ;"] * 6  # 54 tokens, 9 distinct k-grams
    ins = filler(4, "v", 5)[:3]
    b = toks_js(rep)
    a = toks_js(rep[:3] + ins + rep[3:])
    assert 40 <= len(b) and len(a) - len(b) <= MAX_GAP_TOKENS
    assert compare(a, b) is None and compare(b, a) is None


def test_lines_threshold_applies_to_both_spans():
    block = filler(6, "l", 13)
    a = toks_js(block)
    b = toks_js([" ".join(block)])  # same tokens on one line
    assert len(a) >= MIN_MATCH_TOKENS
    assert compare(a, b) is None and compare(b, a) is None


def test_gap_bounded_on_each_side():
    x, y = filler(7, "gx", 14), filler(7, "gy", 15)
    ins = filler(5, "gi", 16)
    a = toks_js(x + y)
    b = toks_js(x + ins + y)
    gap = len(toks_js(ins))
    assert MAX_GAP_TOKENS < gap < MAX_DIAG_DRIFT  # drift cap alone would not stop the bridge
    for p, q in ((a, b), (b, a)):
        m = compare(p, q)
        assert m is not None and m.match_type == "exact"
        assert m.tokens < len(toks_js(x + y))  # one block only, the gap is not bridged


def test_paste_with_chance_normalised_neighbours_is_exact():
    func = filler(30, "fn", 17)
    host_a, host_b = filler(60, "ha", 18), filler(60, "hb", 19)
    a_lines = host_a[:30] + ["alpha ( beta"] + func + ["eps ( zeta ;"] + host_a[30:]
    b_lines = host_b[:20] + ["gamma ( delta"] + func + ["eta ( theta ;"] + host_b[20:]
    a, b = toks_js(a_lines), toks_js(b_lines)
    for p, q, off_p, off_q in ((a, b, 32, 22), (b, a, 22, 32)):
        m = compare(p, q)
        assert m is not None and m.match_type == "exact", m
        assert token_line_span(p, m.a_start, m.a_end) == (off_p, off_p + 29)
        assert token_line_span(q, m.b_start, m.b_end) == (off_q, off_q + 29)


def test_renamed_copy_inside_hosts_stays_normalised():
    c10 = toks_js(read_lines(next((CASES / "C10").glob("*.js"))))
    host = filler(80, "rh", 20)
    m = compare(toks_js(host[:40] + c1() + host[40:]), c10)
    assert m.match_type == "normalised" and m.tokens == len(c10)


def kw_blocks(t, nb):
    tmpl = [lambda t, i: f"    {t}x{i} = kwargs.get('{t}k{i}', {i})",
            lambda t, i: f"    parser.add_argument('--{t}o{i}', type=int, default={i}, help='{t}h{i}')",
            lambda t, i: f"    assert {t}f{i}({i}) == {i}, '{t}m{i}'",
            lambda t, i: f"    log.info('{t}s{i}', {t}v{i}); {t}c += {i}"]
    out = []
    for k in range(nb):
        out += [f"def setup_{t}{k}(kwargs):"] + [tmpl[k](t, i) for i in range(8)] + [""]
    return out


REAL_A = ["def merge_runs(runs, gap=2):", "    runs = sorted(runs)", "    out = [list(runs[0])]",
          "    for lo, hi in runs[1:]:", "        if lo - out[-1][1] <= gap:",
          "            out[-1][1] = max(out[-1][1], hi)", "        else:", "            out.append([lo, hi])",
          "    return [tuple(r) for r in out]"]
REAL_B = [l.replace("runs", "segs").replace("out", "acc").replace("gap", "tol").replace("lo", "s")
          .replace("hi", "e") for l in REAL_A]


def test_renamed_function_after_boilerplate_blocks_found():
    for nb in (2, 3, 4):
        a = tokenize(kw_blocks("a", nb) + REAL_A, "hash")
        b = tokenize(kw_blocks("b", nb) + REAL_B, "hash")
        for p, q in ((a, b), (b, a)):
            m = compare(p, q)
            assert m is not None and m.match_type == "normalised", nb
            first = len(kw_blocks("a", nb)) + 1  # the function starts here (+/- the block seam)
            assert first - 2 <= token_line_span(p, m.a_start, m.a_end)[0] <= first, nb
            assert len(tokenize(REAL_A, "hash")) <= m.tokens <= len(tokenize(REAL_A, "hash")) + 12


def _stdlib_all_lines(name):
    import sysconfig
    path = Path(sysconfig.get_paths()["stdlib"]) / name
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("__all__"))
    end = next(i for i in range(start, len(lines)) if lines[i].strip().endswith("]"))
    return lines[start:end + 1]


def test_stdlib_all_lists_stay_none():
    x, y = _stdlib_all_lines("typing.py"), _stdlib_all_lines("argparse.py")
    if x is None or y is None:
        import pytest
        pytest.skip("stdlib sources not available")
    a, b = tokenize(x, "hash"), tokenize(y, "hash")
    assert compare(a, b) is None and compare(b, a) is None


def test_retry_does_not_resurrect_boilerplate_negatives():
    a = tokenize(["__all__ = ["] + [f"    'Alpha{i}'," for i in range(80)] + ["]"] + kw_blocks("a", 2), "hash")
    b = tokenize(["__all__ = ["] + [f"    'Beta{i}'," for i in range(70)] + ["]"] + kw_blocks("b", 2), "hash")
    assert compare(a, b) is None
    t1 = [f"    self.v{i} = kwargs.get('v{i}', {i})" for i in range(30)]
    t2 = ["def f(self, **kwargs):", "    self.color = kwargs.get('color', 0)",
          "    self.size = kwargs.get('size', 1)", "    return self"]
    assert compare(tokenize(t1, "hash"), tokenize(t2, "hash")) is None


def test_noisy_prefix_does_not_disable_bridging():
    rnd = random.Random(3)
    noise = [" ".join(rnd.choice("();") for _ in range(10)) for _ in range(8000)]
    orig = c1()
    a = toks_js(noise + orig)
    b = toks_js(noise[::-1] + orig[:10] + ["  n++;"] * 12 + orig[10:])
    ms = compare_all(a, b)
    assert any(m.match_type == "near" and m.a_end - m.a_start >= 60 and m.a_end == len(a) for m in ms)
