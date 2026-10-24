"""Winnowing fingerprints and verbatim / near-verbatim overlap between two word-key sequences.

Winnowing (Schleimer, Wilkerson, Aiken 2003): hash every SHINGLE_K-word shingle, keep the rightmost
minimum hash in every window of WINNOW_W shingles. Any shared run of at least WINNOW_W + SHINGLE_K - 1
words shares at least one fingerprint. Shared fingerprints are seeds that are extended into maximal
exact runs; runs separated by at most MAX_GAP_WORDS words on both sides are merged (near_verbatim).
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textnorm import STOPWORDS  # noqa: E402

SHINGLE_K = 5
WINNOW_W = 4
MIN_MATCH_WORDS = 12
MIN_CONTENT_WORDS = 5
MAX_GAP_WORDS = 3
MAX_SEED_POSITIONS = 30     # a fingerprint repeated more often than this on either side is not a useful seed


@dataclass
class Match:
    doc_start: int
    doc_end: int
    src_start: int
    src_end: int
    matched_words: int
    longest_run: int
    kind: str  # verbatim | near_verbatim


def _hash(shingle: list[str]) -> int:
    return int.from_bytes(hashlib.blake2b("\x1f".join(shingle).encode("utf-8"), digest_size=8).digest(), "big")


def fingerprints(keys: list[str]) -> dict[int, list[int]]:
    """Winnowed fingerprints: hash -> start positions (in words) of the selected shingles."""
    n = len(keys) - SHINGLE_K + 1
    if n <= 0:
        return {}
    hashes = [_hash(keys[i:i + SHINGLE_K]) for i in range(n)]
    out: dict[int, list[int]] = {}
    chosen: set[int] = set()
    w = min(WINNOW_W, n)
    for i in range(n - w + 1):
        window = hashes[i:i + w]
        m = min(window)
        pos = i + max(j for j, h in enumerate(window) if h == m)  # rightmost minimum
        if pos not in chosen:
            chosen.add(pos)
            out.setdefault(m, []).append(pos)
    return out


def _extend(doc: list[str], src: list[str], d: int, s: int) -> tuple[int, int, int]:
    """Maximal exact run containing the seed shingle at (d, s): returns (doc_start, src_start, length)."""
    a, b = d, s
    while a > 0 and b > 0 and doc[a - 1] == src[b - 1]:
        a -= 1
        b -= 1
    e, f = d + SHINGLE_K, s + SHINGLE_K
    while e < len(doc) and f < len(src) and doc[e] == src[f]:
        e += 1
        f += 1
    return a, b, e - a


def runs_from_seeds(doc: list[str], src: list[str], seeds: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    covered: dict[int, int] = {}   # diagonal -> doc end of the last run found on it
    runs: list[tuple[int, int, int]] = []
    for d, s in sorted(seeds):
        if doc[d:d + SHINGLE_K] != src[s:s + SHINGLE_K]:
            continue  # hash collision
        diag = d - s
        if covered.get(diag, -1) >= d + SHINGLE_K:
            continue
        a, b, length = _extend(doc, src, d, s)
        covered[diag] = a + length
        runs.append((a, b, length))
    return runs


def build_matches(doc: list[str], runs: list[tuple[int, int, int]], min_words: int = MIN_MATCH_WORDS) -> list[Match]:
    if not runs:
        return []
    runs = sorted(set(runs))
    groups: list[list[tuple[int, int, int]]] = []
    for run in runs:
        placed = False
        if groups:
            last = groups[-1][-1]
            doc_gap = run[0] - (last[0] + last[2])
            src_gap = run[1] - (last[1] + last[2])
            if 0 <= doc_gap <= MAX_GAP_WORDS and 0 <= src_gap <= MAX_GAP_WORDS:
                groups[-1].append(run)
                placed = True
        if not placed:
            groups.append([run])
    matches: list[Match] = []
    for g in groups:
        matched = sum(r[2] for r in g)
        if matched < min_words:
            continue
        content = sum(1 for r in g for k in doc[r[0]:r[0] + r[2]] if k not in STOPWORDS)
        if content < MIN_CONTENT_WORDS:
            continue
        first, last = g[0], g[-1]
        matches.append(Match(first[0], last[0] + last[2], first[1], last[1] + last[2], matched,
                             max(r[2] for r in g), "verbatim" if len(g) == 1 else "near_verbatim"))
    return matches


def find_matches(doc_keys: list[str], src_keys: list[str], min_words: int = MIN_MATCH_WORDS) -> list[Match]:
    fd, fs = fingerprints(doc_keys), fingerprints(src_keys)
    seeds: list[tuple[int, int]] = []
    for h, dpos in fd.items():
        spos = fs.get(h)
        if not spos or len(dpos) > MAX_SEED_POSITIONS or len(spos) > MAX_SEED_POSITIONS:
            continue
        seeds.extend((d, s) for d in dpos for s in spos)
    return build_matches(doc_keys, runs_from_seeds(doc_keys, src_keys, seeds), min_words)


class SourceIndex:
    """One inverted fingerprint index over many sources; the document is then matched against all of them.

    Cost is linear in the total number of fingerprints, not in sources x document seeds."""

    def __init__(self) -> None:
        self.keys: dict[str, list[str]] = {}
        self.inverted: dict[int, list[tuple[str, int]]] = {}

    def add(self, source_id: str, keys: list[str]) -> None:
        self.keys[source_id] = keys
        for h, positions in fingerprints(keys).items():
            bucket = self.inverted.setdefault(h, [])
            bucket.extend((source_id, p) for p in positions)

    def find(self, doc_keys: list[str], min_words: int = MIN_MATCH_WORDS) -> dict[str, list[Match]]:
        seeds: dict[str, list[tuple[int, int]]] = {}
        for h, dpos in fingerprints(doc_keys).items():
            hits = self.inverted.get(h)
            if not hits or len(dpos) > MAX_SEED_POSITIONS:
                continue
            per_source: dict[str, list[int]] = {}
            for sid, p in hits:
                per_source.setdefault(sid, []).append(p)
            for sid, spos in per_source.items():
                if len(spos) > MAX_SEED_POSITIONS:
                    continue
                seeds.setdefault(sid, []).extend((d, s) for d in dpos for s in spos)
        out: dict[str, list[Match]] = {}
        for sid, sd in seeds.items():
            matches = build_matches(doc_keys, runs_from_seeds(doc_keys, self.keys[sid], sd), min_words)
            if matches:
                out[sid] = matches
        return out
