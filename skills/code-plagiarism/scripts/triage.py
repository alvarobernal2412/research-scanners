"""Triage: split files into comparable blocks and drop what cannot be a finding.

Rules only; nothing here touches the network.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from headers import detect_header  # noqa: E402
from inventory import VENDOR_DIRS, SourceFile  # noqa: E402
from model import RawMatch  # noqa: E402
from normalize import LocalMatch  # noqa: E402

MIN_SIGNIFICANT_LINES = 6
MAX_BLOCK_LINES = 60
BOILERPLATE_MIN_RATIO = 0.6
SCANOSS_MIN_MATCHED_PCT = 50
SCANOSS_MIN_LINES = 8
MERGE_GAP_LINES = 3
WEAK_NEAR_MAX_TOKENS = 120
WEAK_NEAR_MIN_COVERAGE = 0.8
MAX_STUB_BODY_LINES = 3

SUPPORTED_LANGS = ("c_like", "hash")
_PROBE = 240  # regexes only ever see this many characters of a line

_DEF_START = re.compile(
    r"(?:async\s+def|async\s+function|def|class|function|func|fn|public|private|protected"
    r"|static|export)\b|const\s.*=>")
_BLANK_RUN_SPLIT = 2
_COMMENT = {
    "c_like": re.compile(r"\s*(?://|/\*|\*)"),
    "hash": re.compile(r"\s*#"),
}
_IMPORT = re.compile(
    r"\s*(?:import\b|from\s+\S+\s+import\b|#\s*include\b|using\b|package\b|use\s+\S+;"
    r"|export\s+.*\bfrom\b)")
_BRACES_ONLY = re.compile(r"[\s{}()\[\];,]*\Z")
_NOT_ACCESSOR = r"(?!(?:return|new|else|throw|await|yield)\b)"
_ACCESSOR = re.compile(
    r"\s*(?:(?:public|private|protected|static|final|async|def|function|func|fn|virtual"
    r"|override|inline|abstract)\s+)*"
    + _NOT_ACCESSOR + r"(?:[\w<>\[\]?*&.:,]+\s+)?(?:get|set|is)[A-Z_]\w*\s*\(")
_MAIN_DEF = re.compile(
    r"\s*(?:(?:public|private|static|async|def|func|fn|void|int)\s+)*main\s*\(")
_MAIN_GUARD = re.compile(r"\s*if\s+__name__\s*==\s*['\"]__main__['\"]\s*:")
_LEAD_WS = re.compile(r"[ \t]*")


@dataclass
class Block:
    rel: str
    start: int   # 1-based, inclusive
    end: int
    lang: str
    lines: list[str]


def _probe(line: str) -> str:
    return line[:_PROBE]


def _is_blank(line: str) -> bool:
    return not line.strip()


def _starts_definition(line: str, prev: str | None) -> bool:
    if line[:1] in (" ", "\t") or not line:
        return False
    if prev is not None and prev.startswith("@"):
        return False  # decorator belongs with its def
    return bool(_DEF_START.match(_probe(line)))


def _chunk(lo: int, hi: int, lines: list[str]) -> list[tuple[int, int]]:
    """Split 0-based [lo, hi] into pieces of at most MAX_BLOCK_LINES lines."""
    out = []
    while hi - lo + 1 > MAX_BLOCK_LINES:
        limit = lo + MAX_BLOCK_LINES - 1
        cut = limit
        for i in range(limit, lo + MAX_BLOCK_LINES // 2, -1):
            if _is_blank(lines[i]):
                cut = i - 1  # end before the blank line
                break
        if cut < lo:
            cut = limit
        out.append((lo, cut))
        lo = cut + 1
        while lo <= hi and _is_blank(lines[lo]) and lo < limit:
            lo += 1
    out.append((lo, hi))
    return out


def split_blocks(sf: SourceFile, lines: list[str]) -> list[Block]:
    """Blocks start at a column-0 definition or after 2+ blank lines.

    Unknown language families yield no blocks (the file is skipped).
    """
    if sf.lang not in SUPPORTED_LANGS or not lines:
        return []
    spans: list[tuple[int, int]] = []
    start: int | None = None
    last = -1       # last non-blank line index in the current block
    blanks = 0
    prev: str | None = None
    for i, line in enumerate(lines):
        if _is_blank(line):
            blanks += 1
            prev = line
            continue
        if start is None:
            start = i
        elif blanks >= _BLANK_RUN_SPLIT or _starts_definition(line, prev):
            spans.append((start, last))
            start = i
        last = i
        blanks = 0
        prev = line
    if start is not None:
        spans.append((start, last))
    blocks = []
    for lo, hi in spans:
        for a, b in _chunk(lo, hi, lines):
            blocks.append(Block(sf.rel, a + 1, b + 1, sf.lang, lines[a:b + 1]))
    return blocks


def _significant_flags(block: Block) -> list[bool]:
    comment = _COMMENT.get(block.lang)
    flags = []
    for line in block.lines:
        p = _probe(line)
        flags.append(not (
            not p.strip()
            or (comment is not None and comment.match(p))
            or _IMPORT.match(p)
            or _BRACES_ONLY.match(p)))
    return flags


def significant_lines(block: Block) -> int:
    return sum(_significant_flags(block))


def _indent(line: str) -> int:
    return _LEAD_WS.match(line).end()


def _body_end(lines: list[str], i: int) -> int:
    """Index after the last line of the body that follows the definition at i."""
    base = _indent(lines[i])
    j = i + 1
    while j < len(lines) and (_is_blank(lines[j]) or _indent(lines[j]) > base):
        j += 1
    return j


def _boilerplate_flags(block: Block, sig: list[bool]) -> list[bool]:
    lines = block.lines
    flags = [False] * len(lines)
    i = 0
    while i < len(lines):
        p = _probe(lines[i])
        accessor = bool(_ACCESSOR.match(p))
        stub = bool(_MAIN_DEF.match(p) or _MAIN_GUARD.match(p))
        if not (sig[i] and (accessor or stub)):
            i += 1
            continue
        end = _body_end(lines, i)
        body = sum(1 for k in range(i + 1, end) if sig[k])
        inline = "{" in p and "}" in p
        if inline or (body == 1 and accessor) or (body <= MAX_STUB_BODY_LINES and stub):
            for k in range(i, end):
                flags[k] = sig[k]
            i = max(end, i + 1)
        else:
            i += 1
    return flags


def trivial_reason(block: Block) -> str | None:
    """license_header | too_short | boilerplate, or None for a real candidate."""
    sig = _significant_flags(block)
    n = sum(sig)
    if n < MIN_SIGNIFICANT_LINES:
        if any(e.kind in ("spdx_tag", "license_text", "copyright")
               for e in detect_header(block.lines)):
            return "license_header"
        return "too_short"
    flags = _boilerplate_flags(block, sig)
    if sum(flags) / n >= BOILERPLATE_MIN_RATIO:
        return "boilerplate"
    return None


def scanoss_drop_reason(match: dict) -> str | None:
    """below_threshold for snippet matches under 50% matched or under 8 lines."""
    if match.get("id") == "file":
        return None
    pm = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%\s*", str(match.get("matched", "")))
    if pm and float(pm.group(1)) < SCANOSS_MIN_MATCHED_PCT:
        return "below_threshold"
    lm = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(match.get("lines", "")))
    if lm and int(lm.group(2)) - int(lm.group(1)) + 1 < SCANOSS_MIN_LINES:
        return "below_threshold"
    return None


def local_match_drop_reason(match: LocalMatch, a_tokens: int) -> str | None:
    """weak_near for short, low-coverage near matches (import floods, idioms).

    a_tokens (size of the user's file in tokens) is accepted for callers that
    want to rate matches against file size; the current rule does not use it.
    """
    if (match.match_type == "near" and match.tokens < WEAK_NEAR_MAX_TOKENS
            and match.coverage < WEAK_NEAR_MIN_COVERAGE):
        return "weak_near"
    return None


def declared_tree_reason(rel: str) -> str | None:
    parts = PurePosixPath(rel.replace("\\", "/")).parts[:-1]
    return "declared_dependency" if any(p in VENDOR_DIRS for p in parts) else None


_TYPE_RANK = {"exact": 0, "normalised": 1, "near": 2}


def merge_adjacent(matches: list[RawMatch]) -> list[RawMatch]:
    """Join matches of one file and origin that overlap or sit within 3 lines."""
    groups: dict[tuple, list[RawMatch]] = {}
    for m in matches:
        key = (m.rel, m.candidate.origin_url, m.candidate.backend, m.match_type == "file")
        groups.setdefault(key, []).append(m)
    out: list[RawMatch] = []
    for group in groups.values():
        group.sort(key=lambda m: (m.start, m.end))
        cur = group[0]
        for m in group[1:]:
            if m.start - cur.end - 1 <= MERGE_GAP_LINES:
                weakest = max((cur.match_type, m.match_type),
                              key=lambda t: _TYPE_RANK.get(t, -1))
                pcts = [p for p in (cur.candidate.matched_pct, m.candidate.matched_pct)
                        if p is not None]
                cand = replace(cur.candidate, matched_pct=min(pcts) if pcts else None,
                               oss_lines=None)
                cur = RawMatch(cur.rel, cur.start, max(cur.end, m.end), weakest, cand)
            else:
                out.append(cur)
                cur = m
        out.append(cur)
    out.sort(key=lambda m: (m.rel, m.start, m.end))
    return out
