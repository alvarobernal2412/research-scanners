"""Pattern schema and JSON loader."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REQUIRED = ("id", "category", "regex", "weight", "source", "explanation", "example")
WIKIPEDIA_FILE = "patterns_wikipedia.json"


@dataclass
class Pattern:
    id: str
    category: str
    regex: str | None
    weight: float
    source: str
    explanation: str
    example: str
    contested: bool = False
    profiles: list[str] | None = None


def load_patterns(dir: Path, include_wikipedia: bool = True) -> list[Pattern]:
    out: list[Pattern] = []
    for f in sorted(Path(dir).glob("*.json")):
        if not include_wikipedia and f.name == WIKIPEDIA_FILE:
            continue
        data = json.loads(f.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError(f"{f.name}: expected a JSON list of patterns")
        for i, raw in enumerate(data):
            pid = raw.get("id", f"<{f.name}#{i}>") if isinstance(raw, dict) else f"<{f.name}#{i}>"
            if not isinstance(raw, dict):
                raise ValueError(f"pattern {pid}: not an object")
            for key in REQUIRED:
                if key not in raw:
                    raise ValueError(f"pattern {pid}: missing field '{key}'")
            if raw["regex"] is not None:
                try:
                    re.compile(raw["regex"])
                except re.error as e:
                    raise ValueError(f"pattern {pid}: invalid regex: {e}") from e
            out.append(
                Pattern(
                    id=raw["id"],
                    category=raw["category"],
                    regex=raw["regex"],
                    weight=raw["weight"],
                    source=raw["source"],
                    explanation=raw["explanation"],
                    example=raw["example"],
                    contested=raw.get("contested", False),
                    profiles=raw.get("profiles"),
                )
            )
    return out
