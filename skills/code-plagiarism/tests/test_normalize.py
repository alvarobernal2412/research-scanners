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


def test_constants():
    assert (K_GRAM, MAX_GAP_TOKENS, MIN_MATCH_TOKENS) == (10, 8, 40)


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


def test_inserted_lines_is_near():
    orig = c1()
    mod = orig[:10] + ["  n++;", "  n++;"] + orig[10:]
    a, b = tokenize(orig, "c_like"), tokenize(mod, "c_like")
    for x, y in ((a, b), (b, a)):
        m = compare(x, y)
        assert m is not None and m.match_type == "near"
        assert 0.5 <= m.coverage < FULL_COVERAGE


def test_original_code_no_match():
    c8 = tokenize(read_lines(next((CASES / "C8").glob("*.py"))), "hash")
    assert compare(tokenize(c1(), "c_like"), c8) is None


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
    a = tokenize([f"v{i} = f(g{i}, {i});" for i in range(6000)], "c_like")
    b = tokenize([f"v{i} = f(g{i}, {i});" for i in range(6000)], "c_like")
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
