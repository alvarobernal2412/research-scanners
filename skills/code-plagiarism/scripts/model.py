"""Shared match model and the backend results-file shape.

A Candidate describes one possible origin; a RawMatch ties it to a line range in
a user file. Backends write results files; report.py reads them.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REQUIRED_RESULT_KEYS = ("backend", "plan_sha256", "root")


@dataclass
class Candidate:
    origin_url: str
    kind: str      # stackoverflow | repository | package
    backend: str   # scanoss | stackexchange
    author: str | None = None
    author_url: str | None = None
    date: str | None = None
    revision: str | None = None
    # items: {spdx, status: declared|none_found|unknown, evidence, confidence}
    licenses: list[dict] = field(default_factory=list)
    matched_pct: float | None = None
    oss_lines: str | None = None
    purl: list[str] = field(default_factory=list)
    post_id: int | None = None
    note: str | None = None


@dataclass
class RawMatch:
    rel: str
    start: int
    end: int
    match_type: str  # exact | normalised | near | file
    candidate: Candidate


def raw_match_to_dict(m: RawMatch) -> dict:
    return asdict(m)


def raw_match_from_dict(d: dict) -> RawMatch:
    known = {f.name for f in fields(Candidate)}
    c = {k: v for k, v in d["candidate"].items() if k in known}
    for key in ("licenses", "purl"):
        c[key] = list(c.get(key) or [])
    return RawMatch(rel=d["rel"], start=int(d["start"]), end=int(d["end"]),
                    match_type=d["match_type"], candidate=Candidate(**c))


def new_results(backend: str, tier: str, plan_sha256: str, root: str) -> dict:
    return {
        "backend": backend, "tier": tier, "plan_sha256": plan_sha256, "root": root,
        "requests_sent": [], "not_sent": [], "matches": [], "dropped": [],
        "dependencies": [], "kb_version": None, "errors": [], "notes": [],
    }


def write_results(path: Path, results: dict) -> None:
    Path(path).write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8", newline="\n")


def load_results(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("results file must be a JSON object")
    missing = [k for k in REQUIRED_RESULT_KEYS if k not in data]
    if missing:
        raise ValueError("results file is missing: " + ", ".join(missing))
    return data
