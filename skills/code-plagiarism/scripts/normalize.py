"""Tokenising, normalisation and local comparison of source code.

Tokeniser: a hand-written scanner loop over one joined text. Every regex used
is a bounded single-class alternation (no nested quantifiers), so hostile input
(1 MB lines, unterminated strings or comments, deep brackets) stays linear.
Comments are dropped; strings are single tokens; lines are 1-based.

Comparison: k-gram hash index (not winnowing). The k-grams (K_GRAM) of the
identifier/string/number-normalised stream of ``b`` go into a dict that keeps at
most a few positions per k-gram, which bounds work on repetitive input. Every
start position of ``a`` whose k-gram is in the index is "marked"; marked windows
are merged into regions when the uncovered gap is at most MAX_GAP_TOKENS, and
the longest region wins. The matching span in ``b`` comes from the occurrences
nearest to a running diagonal (far outliers dropped), so a relocated
common k-gram cannot inflate the span. Coverage is matched tokens divided by the
longer of the two spans, so insertions on either side lower it.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

K_GRAM = 10
MAX_GAP_TOKENS = 8
MIN_MATCH_TOKENS = 40
MIN_MATCH_LINES = 5
FULL_COVERAGE = 0.9
NEAR_MIN_COVERAGE = 0.5
_MAX_POSITIONS = 8      # occurrences kept per k-gram in the index
_MAX_DIAG_DRIFT = 256   # tokens a chosen occurrence may stray from the median diagonal

_KEYWORDS: dict[str, frozenset[str]] = {}
_SCAN = re.compile(
    r"(?P<nl>\n)|(?P<ws>[^\S\n]+)|(?P<id>[^\W\d][\w$]*|\$[\w$]*)|(?P<num>\d[\w.]*)|(?P<ch>.)",
    re.DOTALL)
_STR_BODY = {
    q: re.compile(re.escape(q) + r"(?:[^" + re.escape(q) + r"\\\n]|\\.)*" + re.escape(q))
    for q in ("'", '"')
}
_TICK_BODY = re.compile(r"`(?:[^`\\]|\\.)*`", re.DOTALL)


@dataclass
class Token:
    text: str
    kind: str  # ident|keyword|string|number|op
    line: int


@dataclass
class LocalMatch:
    match_type: str  # exact|normalised|near
    a_start: int
    a_end: int
    b_start: int
    b_end: int
    tokens: int
    coverage: float


def _keywords(lang: str) -> frozenset[str]:
    if not _KEYWORDS:
        path = Path(__file__).resolve().parent.parent / "data" / "keywords.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in data.items():
            _KEYWORDS[k] = frozenset(v)
    return _KEYWORDS[lang]


def tokenize(lines: list[str], lang: str) -> list[Token]:
    if lang not in ("c_like", "hash"):
        raise ValueError(f"unknown language family: {lang}")
    kw = _keywords(lang)
    c_like = lang == "c_like"
    text = "\n".join(lines)
    n = len(text)
    out: list[Token] = []
    append = out.append
    match = _SCAN.match
    find = text.find
    pos = 0
    line = 1
    eol_cache = -1
    failed: dict[str, int] = {}  # quote -> end of line where no closing quote exists

    while pos < n:
        m = match(text, pos)
        kind = m.lastgroup
        end = m.end()
        if kind == "nl":
            line += 1
        elif kind == "ws":
            pass
        elif kind == "id":
            word = m.group()
            append(Token(word, "keyword" if word in kw else "ident", line))
        elif kind == "num":
            append(Token(m.group(), "number", line))
        else:
            c = text[pos]
            nxt = text[pos + 1] if pos + 1 < n else ""
            if (c_like and c == "/" and nxt == "/") or (not c_like and c == "#"):
                end = find("\n", pos)
                end = n if end < 0 else end
            elif c_like and c == "/" and nxt == "*":
                end = find("*/", pos + 2)
                end = n if end < 0 else end + 2
                line += text.count("\n", pos, end)
            elif c in "'\"" and not c_like and text.startswith(c * 3, pos):
                end = find(c * 3, pos + 3)
                end = n if end < 0 else end + 3
                append(Token(text[pos:end], "string", line))
                line += text.count("\n", pos, end)
            elif c in "'\"":
                if eol_cache < pos:  # cached: a fresh find per quote would be quadratic
                    eol_cache = find("\n", pos)
                    eol_cache = n if eol_cache < 0 else eol_cache
                eol = eol_cache
                sm = _STR_BODY[c].match(text, pos, eol) if failed.get(c, -1) < pos else None
                if sm:
                    end = sm.end()
                    append(Token(text[pos:end], "string", line))
                else:
                    failed[c] = eol
                    append(Token(c, "op", line))
            elif c == "`" and c_like:
                sm = _TICK_BODY.match(text, pos)
                end = sm.end() if sm else n
                append(Token(text[pos:end], "string", line))
                line += text.count("\n", pos, end)
            else:
                append(Token(c, "op", line))
        pos = end
    return out


_RENAME = {"ident": "$I", "string": "$S", "number": "$N"}


def normalise(tokens: list[Token], renamed: bool) -> list[str]:
    if not renamed:
        return [t.text for t in tokens]
    return [_RENAME.get(t.kind, t.text) for t in tokens]


def token_line_span(tokens: list[Token], start: int, end: int) -> tuple[int, int]:
    """(first_line, last_line) of tokens[start:end]; (0, 0) when empty."""
    if not tokens or start < 0 or end > len(tokens) or start >= end:
        return (0, 0)
    return (tokens[start].line, tokens[end - 1].line)


def _kgrams(stream: list[str]) -> list[tuple[str, ...]]:
    return [tuple(stream[i:i + K_GRAM]) for i in range(len(stream) - K_GRAM + 1)]


def compare(a: list[Token], b: list[Token]) -> LocalMatch | None:
    if len(a) < K_GRAM or len(b) < K_GRAM:
        return None
    na, nb = normalise(a, True), normalise(b, True)
    index: dict[tuple[str, ...], list[int]] = {}
    gb = _kgrams(nb)
    for j, g in enumerate(gb):
        lst = index.setdefault(g, [])
        if len(lst) < _MAX_POSITIONS:
            lst.append(j)
    ga = _kgrams(na)
    marks = [(i, index[g]) for i, g in enumerate(ga) if g in index]
    if not marks:
        return None

    # Merge marked windows into regions; keep the longest.
    regions = []
    r_lo, r_hi, first = marks[0][0], marks[0][0] + K_GRAM, 0
    for idx in range(1, len(marks)):
        i = marks[idx][0]
        if i - r_hi <= MAX_GAP_TOKENS:
            r_hi = i + K_GRAM
        else:
            regions.append((r_lo, r_hi, first, idx))
            r_lo, r_hi, first = i, i + K_GRAM, idx
    regions.append((r_lo, r_hi, first, len(marks)))
    a_start, a_end, f, e = max(regions, key=lambda r: r[1] - r[0])
    if a_end - a_start < MIN_MATCH_TOKENS:
        return None
    first_line, last_line = token_line_span(a, a_start, a_end)
    if last_line - first_line + 1 < MIN_MATCH_LINES:
        return None

    # Follow one diagonal through b: reuse the running offset while b's k-gram
    # there is identical, else take the nearest indexed occurrence (re-sync).
    sel = marks[f:e]
    votes: dict[int, int] = {}
    for i, cs in sel[:64]:
        votes[cs[0] - i] = votes.get(cs[0] - i, 0) + 1
    diag = max(votes, key=votes.get)
    kept: list[tuple[int, int]] = []
    for i, cs in sel:
        j = i + diag
        if not (0 <= j < len(gb) and gb[j] == ga[i]):
            j = min(cs, key=lambda x: abs(x - i - diag))
            if abs(j - i - diag) > _MAX_DIAG_DRIFT:
                continue
            diag = j - i
        kept.append((i, j))
    if not kept:
        return None
    b_start = min(c for _, c in kept)
    b_end = max(c for _, c in kept) + K_GRAM
    covered = 0
    prev_end = a_start
    for i, _ in kept:
        covered += i + K_GRAM - max(i, prev_end)
        prev_end = i + K_GRAM
    a_len, b_len = a_end - a_start, b_end - b_start
    coverage = min(1.0, covered / max(a_len, b_len))
    if coverage < NEAR_MIN_COVERAGE:
        return None
    mtype = "near"
    if coverage >= FULL_COVERAGE and a_len == b_len:
        if normalise(a[a_start:a_end], False) == normalise(b[b_start:b_end], False):
            mtype = "exact"
        elif na[a_start:a_end] == nb[b_start:b_end]:
            mtype = "normalised"
    return LocalMatch(mtype, a_start, a_end, b_start, b_end, covered, round(coverage, 4))
