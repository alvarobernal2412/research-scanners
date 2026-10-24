"""Word normalisation, tokenisation with source positions, sentence splitting and glue detection.

Normalisation order that made all 15 spike sentences match: NFKC, casefold, curly to straight quotes,
edge punctuation stripped, hyphens (including soft hyphens) deleted, line-end hyphenation joined.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import Line  # noqa: E402

GLUED_TOKEN_LEN = 20
GLUED_MAX_RATIO = 0.02

_HYPHENS = "-‐‑­"
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                         "“": '"', "”": '"', "„": '"', "«": '"', "»": '"'})
_NONWORD_EDGE = re.compile(r"^[^\w]+|[^\w]+$")
_HYPHEN_DEL = str.maketrans("", "", _HYPHENS)

STOPWORDS = frozenset("""a about above after again against all also am an and any are as at be because been before
being below between both but by can could did do does doing down during each few for from further had has have having
he her here hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now
of off on once only or other our ours ourselves out over own same she should so some such than that the their theirs
them themselves then there these they this those through to too under until up us very was we were what when where
which while who whom why will with would you your yours yourself yourselves may might must shall upon within without
whether however thus therefore hence among per via etc eg ie one two three""".split())


@dataclass
class Token:
    key: str
    line: int
    start: int
    end_line: int
    end: int
    raw: str = ""


@dataclass
class Sentence:
    text: str
    start_tok: int
    end_tok: int
    line_start: int
    line_end: int


@lru_cache(maxsize=262144)
def normalise_word(raw: str) -> str:
    if raw.isascii() and raw.isalnum():
        return raw.lower()
    w = unicodedata.normalize("NFKC", raw).casefold().translate(_QUOTES)
    w = w.translate(_HYPHEN_DEL)
    w = _NONWORD_EDGE.sub("", w)
    return w


_WORD = re.compile(r"\S+")


def tokenize(lines: list[Line]) -> list[Token]:
    # raw pieces: (line no, start, end, raw text)
    pieces: list[tuple[int, int, int, str]] = []
    for ln in lines:
        for m in _WORD.finditer(ln.text):
            pieces.append((ln.no, m.start(), m.end(), m.group()))
    # join lone hyphens between lower-case words on the same line ("data - driven")
    out: list[Token] = []
    i = 0
    n = len(pieces)
    last_on_line: dict[int, int] = {}
    for idx, p in enumerate(pieces):
        last_on_line[p[0]] = idx
    while i < n:
        no, s, e, raw = pieces[i]
        # line-end hyphenation: "pre-" + "vious" on the next non-empty line
        if (raw[-1:] in _HYPHENS and len(raw) > 1 and last_on_line[no] == i and i + 1 < n
                and pieces[i + 1][3][:1].islower() and normalise_word(raw)):
            nno, ns, ne, nraw = pieces[i + 1]
            joined = raw.rstrip(_HYPHENS) + nraw
            key = normalise_word(joined)
            if key:
                out.append(Token(key, no, s, nno, ne, joined))
            i += 2
            continue
        # a lone hyphen between two lower-case words of one line
        if (raw in ("-", "‐", "‑") and out and i + 1 < n and pieces[i + 1][0] == no
                and out[-1].end_line == no and out[-1].raw[-1:].islower() and pieces[i + 1][3][:1].islower()):
            nno, ns, ne, nraw = pieces[i + 1]
            prev = out.pop()
            joined = prev.raw + nraw
            out.append(Token(normalise_word(joined), prev.line, prev.start, nno, ne, joined))
            i += 2
            continue
        key = normalise_word(raw)
        if key:
            out.append(Token(key, no, s, no, e, raw))
        i += 1
    return out


_ABBREV = {"e.g", "i.e", "et al", "al", "fig", "figs", "cf", "vs", "eq", "eqs", "sec", "sect", "no", "dr", "prof",
           "mr", "mrs", "ms", "approx", "ca", "resp", "ref", "refs", "vol", "pp", "p", "ed", "eds", "st", "inc",
           "etc", "tab", "chap", "viz"}
_END_PUNCT = re.compile(r"[.!?][\"')\]”’»]*$")
_STARTS = re.compile(r"""^[\"'(\[“‘«]*[A-Z0-9]""")


def _is_boundary(prev_raw: str, next_raw: str) -> bool:
    if not _END_PUNCT.search(prev_raw) or not _STARTS.match(next_raw):
        return False
    stem = re.sub(r"[\"')\]”’»]+$", "", prev_raw)
    if not stem.endswith((".", "!", "?")):
        return False
    if stem.endswith(("!", "?")):
        return True
    base = stem[:-1].lstrip("([\"'“‘")
    low = base.casefold()
    if low in _ABBREV or re.fullmatch(r"[A-Za-z]", base):
        return False
    if re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]", base):
        return False  # "e.g." / "i.e." / "U.S."
    if re.fullmatch(r"\d+", base):
        return False  # "1. Introduction"-style numbering inside a line
    return True


def split_sentences(lines: list[Line], tokens: list[Token]) -> list[Sentence]:
    by_no = {l.no: l for l in lines}
    order = {l.no: i for i, l in enumerate(lines)}
    sentences: list[Sentence] = []
    if not tokens:
        return sentences
    start = 0

    def close(a: int, b: int):
        if b <= a:
            return
        parts = [t.raw or by_no[t.line].text[t.start:t.end] for t in tokens[a:b]]
        sentences.append(Sentence(" ".join(parts), a, b, tokens[a].line, tokens[b - 1].end_line))

    for i in range(1, len(tokens)):
        prev, cur = tokens[i - 1], tokens[i]
        split = False
        if cur.line != prev.end_line:
            a, b = order[prev.end_line], order[cur.line]
            gap = lines[a + 1:b]
            if any(not g.text.strip() for g in gap):
                split = True  # a blank line ends the sentence
            elif by_no[cur.line].heading_level is not None or by_no[prev.end_line].heading_level is not None:
                split = True
        if not split and _is_boundary(prev.raw or by_no[prev.end_line].text[prev.start:prev.end], cur.raw):
            split = True
        if split:
            close(start, i)
            start = i
    close(start, len(tokens))
    return sentences


def _looks_glued(key: str) -> bool:
    if len(key) < GLUED_TOKEN_LEN or any(c in key for c in "/:@\\"):
        return False  # URLs, DOIs, e-mail addresses and paths are legitimately long
    return sum(c.isalpha() for c in key) >= 0.8 * len(key)


def glued_ratio(tokens: list[Token]) -> float:
    """Share of tokens that are very long runs of letters: a sign of PDF text with the spaces lost."""
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if _looks_glued(t.key)) / len(tokens)
