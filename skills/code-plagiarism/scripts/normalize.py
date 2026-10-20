"""Tokenising, normalisation and local comparison of source code.

Tokeniser: a hand-written scanner loop over one joined text. Every regex is a
bounded single-class alternation (no nested quantifiers), so hostile input
(1 MB lines, unterminated strings or comments, deep brackets) stays linear.
Comments are dropped; strings are single tokens; lines are 1-based.

Comparison (``compare`` / ``compare_all``), as implemented:

1. Anchors. Every K_GRAM-token window of the identifier/string/number-normalised
   stream is hashed (collisions are re-checked on the token lists). An anchor is
   a pair (i, j) of windows with the same k-gram in ``a`` and ``b``. Windows with
   fewer than MIN_WINDOW_DISTINCT distinct tokens (``( ( ( ;`` noise) never
   anchor. K-grams whose pair count (occurrences in a x occurrences in b) is above
   a cap are boilerplate and are not anchored; the cap shrinks from 64 towards 1
   to fit MAX_ANCHORS, but k-grams unique in both files always anchor, so the
   anchor count is bounded by that cap only, not absolutely (<= min(|a|, |b|)).
2. Diagonal chaining. A chain is a sequence of anchors that strictly increase
   in both ``a`` and ``b``. Between consecutive anchors the unmatched gap on
   EACH side is at most MAX_GAP_TOKENS and the diagonal (j - i) drifts by at
   most MAX_DIAG_DRIFT (implied by the gap limit while MAX_GAP_TOKENS < it);
   overlapping windows must lie on the same diagonal. A dynamic programme
   (same-diagonal predecessor first, otherwise a bounded look-back of
   K_GRAM + MAX_GAP_TOKENS positions) picks the chain with the most matched
   tokens, each exact raw-token window counting double so a later verbatim copy
   beats an earlier renamed one. The look-back has a work reservation of
   MAX_LOOKBACK_WORK = 120_000 steps per block of 2048 anchors (about 60 steps
   per anchor, enough for dense real code); once a block's reservation is spent,
   gaps are no longer bridged for the rest of that block only (anchors there
   start new chains), so a noisy region cannot disable bridging elsewhere.
3. Spans come only from the chosen chain (first/last matched token on each
   side, ``a_end``/``b_end`` exclusive). Each end is then trimmed (up to
   K_GRAM - 1 tokens) where the raw tokens differ, because a chance normalised
   k-gram can reach past a verbatim copy; if the trimmed span is still not
   raw-identical, each end is cut back past the last raw mismatch within
   K_GRAM - 1 tokens and the cut is kept only when that makes the span
   raw-identical (a genuinely renamed copy keeps its span). ``coverage`` =
   matched tokens / max(a span, b span) (1.0 for raw-identical spans), so insertions on either side lower it
   equally and compare(a, b) mirrors compare(b, a). ``LocalMatch.tokens`` is
   that longer span length, not the matched count.
4. Filters. Both spans need >= MIN_MATCH_TOKENS tokens and >= MIN_MATCH_LINES
   lines, and coverage >= NEAR_MIN_COVERAGE. Unless the two spans are
   raw-token-identical (repetitive code copied whole is still a copy), the
   region's k-gram variety (distinct normalised k-grams / total) must be >=
   MIN_KGRAM_VARIETY with >= MIN_DISTINCT_KGRAMS distinct ones on BOTH sides;
   bean getters/setters, data tables and ``__all__`` lists fail it and are
   dropped. When a non-identical region fails only on variety, the search is
   retried once on that region's anchors whose k-gram occurs exactly once in
   each file; the retried region must pass every threshold again and be at
   least MIN_RETRY_TOKENS long, so a renamed function sitting next to
   boilerplate blocks is still found.
5. Type. ``exact``: the raw token texts of both spans are identical and
   coverage >= FULL_COVERAGE; ``normalised``: identical only after renaming;
   otherwise ``near``.
6. ``compare_all`` repeats the chain search, masking each reported region's
   anchors inside its (a span x b span) box, so several regions (swapped
   functions, two copies in ``b``) are returned ordered by (a_start, b_start);
   one ``a`` span may therefore pair with several ``b`` spans. Only
   max_regions + 3 chains are examined per call (rejected chains count), and
   ``compare`` returns the best valid region among them.

Known limits: a k-gram that is common in both files can relocate a chain only
within MAX_DIAG_DRIFT; edits whose gap on either side exceeds MAX_GAP_TOKENS
split the match into separate regions; heavily repeated k-grams never anchor,
so a very repetitive file compared with itself can yield only partial regions
(pair-cap design) and giant identical files are not guaranteed to be found whole;
compare_all(a, b) and compare_all(b, a) can differ in rare ties; pure data tables
and very low-symbol code never anchor (pair cap, MIN_WINDOW_DISTINCT); identical
files of 250k+ tokens return None; small ``near`` hits (about 50-90 tokens at
coverage 0.5-0.8) between unrelated files are chance idioms, left to triage; noise that
yields many long rejected chains can use up the examined-chain budget before a
real region is reached.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

K_GRAM = 10
MAX_GAP_TOKENS = 40     # ruling: raised from the plan's 8; applies to gaps in a and in b
MAX_DIAG_DRIFT = 64
MIN_MATCH_TOKENS = 40
MIN_MATCH_LINES = 5
FULL_COVERAGE = 0.9
NEAR_MIN_COVERAGE = 0.5
MIN_KGRAM_VARIETY = 0.5
MIN_DISTINCT_KGRAMS = 20
MAX_ANCHORS = 250_000
MAX_LOOKBACK_WORK = 120_000  # look-back steps per block of _LOOKBACK_BLOCK anchors (bounds hostile input)
_LOOKBACK_BLOCK = 2048
MIN_WINDOW_DISTINCT = 4  # a k-gram window needs this many distinct normalised tokens to anchor
MIN_RETRY_TOKENS = 60  # a region found only after dropping repeated k-grams must be this long
_PAIR_CAPS = (64, 32, 16, 8, 4, 2, 1)  # max a-occurrences x b-occurrences per anchored k-gram

_KEYWORDS: dict[str, frozenset[str]] = {}
_SCAN = re.compile(
    r"(?P<nl>\n)|(?P<ws>[^\S\n]+)|(?P<id>[^\W\d][\w$]*|\$[\w$]*)|(?P<num>\d[\w.]*)|(?P<op2>\+\+|--|==|!=|<=|>=|&&|\|\||=>|->|::|\+=|-=|\*=|/=|<<|>>)|(?P<ch>.)",
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
        elif kind == "op2":
            append(Token(m.group(), "op", line))
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




def _hashes(stream: list[str]) -> list[int]:
    return [hash(tuple(stream[i:i + K_GRAM])) for i in range(len(stream) - K_GRAM + 1)]


def _anchors(ra, rb, na, nb, ha, hb):
    """Shared k-gram windows (i, j, raw_equal), sorted by (i, j)."""
    idx: dict[int, list[int]] = {}
    for j, h in enumerate(hb):
        idx.setdefault(h, []).append(j)
    cnt: dict[int, int] = {}
    for h in ha:
        cnt[h] = cnt.get(h, 0) + 1
    hist: dict[int, int] = {}  # pairs-per-kgram -> total pairs
    for h, c in cnt.items():
        lb = idx.get(h)
        if lb:
            p = c * len(lb)
            hist[p] = hist.get(p, 0) + p
    cap = 1  # unique-in-both k-grams always anchor
    for cap in _PAIR_CAPS:
        if sum(v for p, v in hist.items() if p <= cap) <= MAX_ANCHORS:
            break
    ai: list[int] = []
    aj: list[int] = []
    ar: list[bool] = []
    for i, h in enumerate(ha):
        lb = idx.get(h)
        if lb is None or cnt[h] * len(lb) > cap:
            continue
        ki = na[i:i + K_GRAM]
        if len(set(ki)) < MIN_WINDOW_DISTINCT:  # e.g. "( ( ( ;": noise, never an anchor
            continue
        for j in lb:
            if nb[j:j + K_GRAM] != ki:  # hash collision
                continue
            ai.append(i)
            aj.append(j)
            ar.append(ra[i:i + K_GRAM] == rb[j:j + K_GRAM])
    return ai, aj, ar


def _best_chain(ai, aj, ar, nwa, nwb):
    """Best-scoring anchor chain as (indexes, matched tokens), or None."""
    n = len(ai)
    if n == 0:
        return None
    starts = [0] * (nwa + 2)
    p = 0
    for x in range(nwa + 1):
        while p < n and ai[p] < x:
            p += 1
        starts[x] = p
    starts[nwa + 1] = n
    width = nwb + 1
    pos = {ai[k] * width + aj[k]: k for k in range(n)}
    score = [0] * n
    matched = [0] * n
    prev = [-1] * n
    window = K_GRAM + MAX_GAP_TOKENS
    quota = 0
    for k in range(n):
        if k % _LOOKBACK_BLOCK == 0:
            quota = MAX_LOOKBACK_WORK  # per-block reservation: noise cannot starve later blocks
        i, j = ai[k], aj[k]
        w = 2 if ar[k] else 1  # exact raw-token windows count double
        q = pos.get((i - 1) * width + j - 1)
        if q is not None:  # continue the same diagonal
            score[k], matched[k], prev[k] = score[q] + w, matched[q] + 1, q
            continue
        best, bm, bp = K_GRAM * w, K_GRAM, -1
        lo = starts[max(i - window, 0)]
        if quota >= k - lo:
            quota -= k - lo
            for q in range(k - 1, lo - 1, -1):
                da = i - ai[q]
                db = j - aj[q]
                if da <= 0 or db <= 0:
                    continue
                if da == db:
                    add = da if da < K_GRAM else K_GRAM
                elif (da < K_GRAM or db < K_GRAM or da - K_GRAM > MAX_GAP_TOKENS
                      or db - K_GRAM > MAX_GAP_TOKENS or abs(da - db) > MAX_DIAG_DRIFT):
                    continue
                else:
                    add = K_GRAM
                c = score[q] + add * w
                if c > best:
                    best, bm, bp = c, matched[q] + add, q
        score[k], matched[k], prev[k] = best, bm, bp
    end = max(range(n), key=score.__getitem__)
    chain = []
    k = end
    while k != -1:
        chain.append(k)
        k = prev[k]
    chain.reverse()
    return chain, matched[end]


def _variety_ok(hashes: list[int], start: int, end: int) -> bool:
    window = hashes[start:end - K_GRAM + 1]
    distinct = len(set(window))
    return distinct >= MIN_DISTINCT_KGRAMS and distinct / max(len(window), 1) >= MIN_KGRAM_VARIETY


def _finish(a, b, ra, rb, na, nb, ha, hb, span, matched, min_tokens):
    """Trim, filter and classify one chain span. Returns (match, variety_rejected)."""
    a_s, a_e, b_s, b_e = span
    # A chance normalised k-gram can reach a few tokens past a verbatim copy:
    # trim ends whose raw tokens differ (at most K_GRAM - 1 tokens per end) ...
    for _ in range(K_GRAM - 1):
        if a_s < a_e - 1 and b_s < b_e - 1 and ra[a_s] != rb[b_s]:
            a_s, b_s, matched = a_s + 1, b_s + 1, matched - 1
    for _ in range(K_GRAM - 1):
        if a_s < a_e - 1 and b_s < b_e - 1 and ra[a_e - 1] != rb[b_e - 1]:
            a_e, b_e, matched = a_e - 1, b_e - 1, matched - 1
    # ... then cut back past the last raw mismatch near each end, keeping the cut
    # only if the remainder is raw-identical (a renamed copy keeps its span).
    if ra[a_s:a_e] != rb[b_s:b_e]:
        span_len = min(K_GRAM - 1, a_e - a_s)
        ts = max([t + 1 for t in range(span_len) if ra[a_s + t] != rb[b_s + t]] or [0])
        te = max([t + 1 for t in range(span_len) if ra[a_e - 1 - t] != rb[b_e - 1 - t]] or [0])
        if a_e - te > a_s + ts and ra[a_s + ts:a_e - te] == rb[b_s + ts:b_e - te]:
            a_s, b_s, a_e, b_e = a_s + ts, b_s + ts, a_e - te, b_e - te
            matched -= ts + te
    a_len, b_len = a_e - a_s, b_e - b_s
    if min(a_len, b_len) < max(MIN_MATCH_TOKENS, min_tokens):
        return None, False
    la, lb = token_line_span(a, a_s, a_e), token_line_span(b, b_s, b_e)
    if min(la[1] - la[0], lb[1] - lb[0]) + 1 < MIN_MATCH_LINES:
        return None, False
    verbatim = ra[a_s:a_e] == rb[b_s:b_e]
    # Raw-identical spans match token for token, even where boilerplate k-grams
    # (dropped by the pair cap) left holes in the anchor chain.
    coverage = 1.0 if verbatim else min(1.0, matched / max(a_len, b_len))
    if coverage < NEAR_MIN_COVERAGE:
        return None, False
    # Variety is required on both sides, except for verbatim copies (repetitive
    # code copied whole is still a copy).
    if not verbatim and not (_variety_ok(ha, a_s, a_e) and _variety_ok(hb, b_s, b_e)):
        return None, True
    mtype = "near"
    if coverage >= FULL_COVERAGE and a_len == b_len:
        if verbatim:
            mtype = "exact"
        elif na[a_s:a_e] == nb[b_s:b_e]:
            mtype = "normalised"
    return LocalMatch(mtype, a_s, a_e, b_s, b_e, max(a_len, b_len), round(coverage, 4)), False


def _overlaps(found, m):
    return any(m.a_start < f.a_end and f.a_start < m.a_end and m.b_start < f.b_end and f.b_start < m.b_end
               for f in found)


def _regions(a: list[Token], b: list[Token], max_regions: int) -> list[LocalMatch]:
    """Valid regions in the order they were found (best first)."""
    if max_regions < 1 or len(a) < K_GRAM or len(b) < K_GRAM:
        return []
    ra, rb = normalise(a, False), normalise(b, False)
    na, nb = normalise(a, True), normalise(b, True)
    ha, hb = _hashes(na), _hashes(nb)
    ai, aj, ar = _anchors(ra, rb, na, nb, ha, hb)
    found: list[LocalMatch] = []
    attempts = 0
    counts = None
    while ai and len(found) < max_regions and attempts < max_regions + 3:
        attempts += 1
        res = _best_chain(ai, aj, ar, len(ha), len(hb))
        if res is None:
            break
        chain, matched = res
        a_s, a_e = ai[chain[0]], ai[chain[-1]] + K_GRAM
        b_s, b_e = aj[chain[0]], aj[chain[-1]] + K_GRAM
        inside = [k for k in range(len(ai))
                  if ai[k] < a_e and ai[k] + K_GRAM > a_s and aj[k] < b_e and aj[k] + K_GRAM > b_s]
        inside_set = set(inside)
        box = [(ai[k], aj[k], ar[k]) for k in inside]
        keep = [k for k in range(len(ai)) if k not in inside_set]
        ai, aj, ar = [ai[k] for k in keep], [aj[k] for k in keep], [ar[k] for k in keep]
        m, low_variety = _finish(a, b, ra, rb, na, nb, ha, hb, (a_s, a_e, b_s, b_e), matched, 0)
        if m is None and low_variety:
            # Boilerplate swallowed the region: retry once on the anchors whose
            # k-gram is unique in both files, with a stricter minimum length.
            if counts is None:
                counts = ({}, {})
                for h in ha:
                    counts[0][h] = counts[0].get(h, 0) + 1
                for h in hb:
                    counts[1][h] = counts[1].get(h, 0) + 1
            uniq = [(i, j, r) for i, j, r in box if counts[0][ha[i]] == 1 and counts[1][hb[j]] == 1]
            if uniq:
                ui, uj, ur = [u[0] for u in uniq], [u[1] for u in uniq], [u[2] for u in uniq]
                res2 = _best_chain(ui, uj, ur, len(ha), len(hb))
                if res2 is not None:
                    c2, matched2 = res2
                    m, _ = _finish(a, b, ra, rb, na, nb, ha, hb,
                                   (ui[c2[0]], ui[c2[-1]] + K_GRAM, uj[c2[0]], uj[c2[-1]] + K_GRAM),
                                   matched2, MIN_RETRY_TOKENS)
        if m is not None and not _overlaps(found, m):
            found.append(m)
    return found


def compare_all(a: list[Token], b: list[Token], max_regions: int = 5) -> list[LocalMatch]:
    """Up to ``max_regions`` regions, ordered by (a_start, b_start)."""
    return sorted(_regions(a, b, max_regions), key=lambda m: (m.a_start, m.b_start))


def compare(a: list[Token], b: list[Token]) -> LocalMatch | None:
    """The best region (most matched, exact-weighted tokens), or None."""
    found = _regions(a, b, 1)
    return found[0] if found else None
