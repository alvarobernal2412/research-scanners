"""Regex scan engine: find pattern hits in extracted lines."""
from __future__ import annotations

import re
from dataclasses import dataclass

from extract import Line
from patterns import Pattern


@dataclass
class Finding:
    line: int
    text: str
    pattern_id: str
    category: str
    source: str
    weight: float
    contested: bool


def find_hits(lines: list[Line], patterns: list[Pattern]) -> list[Finding]:
    compiled = [(p, re.compile(p.regex, re.IGNORECASE)) for p in patterns if p.regex]
    out: list[Finding] = []
    for ln in lines:
        if ln.in_code or ln.in_quote:
            continue
        for p, rx in compiled:
            for m in rx.finditer(ln.text):
                out.append(Finding(ln.no, m.group(0), p.id, p.category, p.source, p.weight, p.contested))
    return out
