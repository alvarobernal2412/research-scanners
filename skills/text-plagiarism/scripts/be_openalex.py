"""OpenAlex open-tier backend: stemmed full-text search over about 17% of works (spike measurement).

A quoted window of about 12 words is sent (commas and pipes removed, no final period); if it finds nothing,
one second window may be tried. Zero results mean "not covered", never "clean". About 40% of true sentences
are never found. The keyless budget is shared and small: the transport honours 429 and `budget_remaining`
lets the open tier stop and record the rest as budget_exhausted.
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

SERVICE = "openalex"
KEYED_ONLY = False
KEY_ENV = "OPENALEX_API_KEY"
BASE = "https://api.openalex.org/works"
WINDOW_WORDS = 12


def _words(phrase: str) -> list[str]:
    return phrase.replace(",", " ").replace("|", " ").rstrip(" .").split()


def window_phrase(sentence: str) -> str:
    """First window: the first ~12 words, no commas, no pipes, no final period."""
    return " ".join(_words(sentence)[:WINDOW_WORDS])


def second_window(sentence: str) -> str | None:
    """A second, different window: the last 12 words, or words 4-16 for shorter sentences."""
    w = _words(sentence)
    if len(w) >= 2 * WINDOW_WORDS:
        return " ".join(w[-WINDOW_WORDS:])
    if len(w) >= 16:
        return " ".join(w[4:16])
    return None


def build_search(phrase: str, req_id: str, use_key: bool = False) -> PlannedRequest:
    clean = " ".join(_words(phrase)).replace('"', " ")
    params = {"filter": f'fulltext.search:"{clean}"', "per-page": "5"}
    identity = None
    url = BASE + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    if use_key and os.environ.get(KEY_ENV):
        url += "&api_key={" + KEY_ENV + "}"
        identity = KEY_ENV
    return PlannedRequest(id=req_id, service=SERVICE, method="GET", url=url, carries="user_text",
                          purpose="search OpenAlex full text for this phrase (stemmed)", text_sent=clean,
                          identity=identity)


def _license(raw: str | None) -> str | None:
    return raw.upper().replace("-", " ") if raw else None


def parse_search(body: bytes, query_id: str) -> tuple[int, list[SearchHit]]:
    data = json.loads(body.decode("utf-8"))
    count = int((data.get("meta") or {}).get("count") or 0)
    hits = []
    for w in data.get("results", []):
        doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
        best = w.get("best_oa_location") or {}
        pdf = best.get("pdf_url") or next((l.get("pdf_url") for l in w.get("locations", []) or []
                                           if l.get("is_oa") and l.get("pdf_url")), None)
        hits.append(SearchHit(query_id=query_id, backend=SERVICE, doi=doi, title=w.get("title"),
                              url=w.get("doi") or w.get("id"), license=_license(best.get("license")),
                              fulltext_url=pdf, notes=["stemmed_search"]))
    return count, hits


def budget_remaining(headers: dict) -> int | None:
    raw = (headers or {}).get("x-ratelimit-remaining")
    try:
        return int(float(raw)) if raw is not None else None
    except ValueError:
        return None
