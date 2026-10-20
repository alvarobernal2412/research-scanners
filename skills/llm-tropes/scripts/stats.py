"""Document-level statistics over extracted lines (stdlib only)."""
from __future__ import annotations

import re
from collections import Counter

MATTR_WINDOW = 50

_WORD = re.compile(r"\b[\w'’-]+\b")
_SENT_SPLIT = re.compile(r"[.!?]+(?:\s+|$)")
_ITEM = r"[\w'’-]+(?:[ \t]+[\w'’-]+){0,3}"
_TRICOLON = re.compile(
    rf"(?<![\w'’-])({_ITEM}),[ \t]+({_ITEM}),?[ \t]+(?:and|or)[ \t]+({_ITEM})(?![\w'’-])"
)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$")

_KEYS = (
    "words",
    "sentence_length_variance",
    "mattr",
    "trigram_repetition",
    "tricolon_per_1k",
    "em_dash_per_1k",
    "bold_first_bullet_ratio",
)


def _mattr(words: list[str]) -> float:
    if not words:
        return 0.0
    if len(words) < MATTR_WINDOW:
        return len(set(words)) / len(words)
    counts = Counter(words[:MATTR_WINDOW])
    total = len(counts) / MATTR_WINDOW
    n = 1
    for i in range(MATTR_WINDOW, len(words)):
        out, inn = words[i - MATTR_WINDOW], words[i]
        counts[out] -= 1
        if counts[out] == 0:
            del counts[out]
        counts[inn] += 1
        total += len(counts) / MATTR_WINDOW
        n += 1
    return total / n


def compute_stats(lines) -> dict:
    stats = {k: 0.0 for k in _KEYS}
    prose = [l.text for l in lines if not l.in_code and not l.in_quote]

    bullets = bold = 0
    for t in prose:
        m = _BULLET.match(t)
        if m:
            bullets += 1
            if m.group(1).startswith("**"):
                bold += 1
    if bullets:
        stats["bold_first_bullet_ratio"] = bold / bullets

    text = "\n".join(prose)
    words = [w.lower() for w in _WORD.findall(text)]
    n = len(words)
    stats["words"] = float(n)
    if n == 0:
        return stats

    counts = [len(_WORD.findall(s)) for s in _SENT_SPLIT.split(text)]
    counts = [c for c in counts if c > 0]
    if len(counts) >= 2:
        mean = sum(counts) / len(counts)
        stats["sentence_length_variance"] = sum((c - mean) ** 2 for c in counts) / len(counts)

    stats["mattr"] = float(_mattr(words))

    if n >= 3:
        tris = Counter(zip(words, words[1:], words[2:]))
        total = n - 2
        stats["trigram_repetition"] = sum(c for c in tris.values() if c > 1) / total

    tri = sum(len(_TRICOLON.findall(t)) for t in prose)
    stats["tricolon_per_1k"] = tri * 1000.0 / n
    stats["em_dash_per_1k"] = text.count("—") * 1000.0 / n
    return stats
