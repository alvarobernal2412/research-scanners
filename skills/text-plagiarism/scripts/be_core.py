"""CORE open-tier backend (UNVERIFIED, opt-in only).

In the fixtures spike CORE answered with 500s and 429s and produced no verified body-text hit, so it is not
a default backend and is not used in acceptance tests. The phrase is sent parenthesised in double quotes and
the URL keeps its trailing slash (both required by the API). Any hit is verified locally like every other.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval import PlannedRequest  # noqa: E402
from hits import SearchHit  # noqa: E402

SERVICE = "core"
KEYED_ONLY = False
KEY_ENV = "CORE_API_KEY"
UNVERIFIED = True
BASE = "https://api.core.ac.uk/v3/search/works/"


def build_search(phrase: str, req_id: str, use_key: bool = False) -> PlannedRequest:
    clean = phrase.replace('"', " ")
    url = BASE + "?" + urllib.parse.urlencode({"q": f'("{clean}")', "limit": "5"}, quote_via=urllib.parse.quote)
    headers: dict[str, str] = {}
    identity = None
    if use_key and os.environ.get(KEY_ENV):
        headers["Authorization"] = "Bearer {" + KEY_ENV + "}"
        identity = KEY_ENV
    return PlannedRequest(id=req_id, service=SERVICE, method="GET", url=url, carries="user_text",
                          purpose="search CORE for this exact phrase (unverified backend)", text_sent=phrase,
                          headers=headers, identity=identity)


def parse_search(body: bytes, query_id: str) -> tuple[int, list[SearchHit]]:
    data = json.loads(body.decode("utf-8"))
    count = int(data.get("totalHits") or 0)
    hits = []
    for w in data.get("results", []) or []:
        doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
        hits.append(SearchHit(query_id=query_id, backend=SERVICE, doi=doi, title=w.get("title"),
                              url=f"https://core.ac.uk/works/{w['id']}" if w.get("id") else None,
                              license=None, fulltext_url=w.get("downloadUrl"), full_text=w.get("fullText") or None,
                              notes=["core_body_text_unverified"]))
    return count, hits
