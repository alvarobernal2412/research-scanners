"""Search hit record shared by the open-tier backends and the verifier."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


@dataclass
class SearchHit:
    query_id: str
    backend: str
    doi: str | None
    title: str | None
    url: str | None
    license: str | None
    pmcid: str | None = None
    fulltext_url: str | None = None
    full_text: str | None = None
    snippet: str | None = None
    notes: list[str] = field(default_factory=list)
