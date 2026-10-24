"""Open-tier backends for books and the open web: Open Library and Brave.

`openlibrary` searches inside digitised books and returns highlighted snippets only, so its hits are always
unverified candidates and never findings. `brave` is keyed-only (BRAVE_API_KEY, header X-Subscription-Token):
its result pages are fetched by the verify round and the overlap is recomputed locally from the page text.
Both are opt-in; neither is in DEFAULT_BACKENDS.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval import PlannedRequest  # noqa: E402
from hits import SearchHit  # noqa: E402


class _OpenLibrary:
    SERVICE = "openlibrary"
    KEYED_ONLY = False
    KEY_ENV = None
    BASE = "https://openlibrary.org/search/inside.json"

    def build_search(self, phrase: str, req_id: str, use_key: bool = False) -> PlannedRequest:
        clean = phrase.replace('"', " ")
        url = self.BASE + "?" + urllib.parse.urlencode({"q": f'"{clean}"'}, quote_via=urllib.parse.quote)
        return PlannedRequest(id=req_id, service=self.SERVICE, method="GET", url=url, carries="user_text",
                              purpose="search inside digitised books (snippets only)", text_sent=phrase)

    def parse_search(self, body: bytes, query_id: str) -> tuple[int, list[SearchHit]]:
        data = json.loads(body.decode("utf-8"))
        block = data.get("hits") or {}
        total = block.get("total")
        count = int(total.get("value") if isinstance(total, dict) else total or 0)
        hits = []
        for h in block.get("hits", []) or []:
            fields = h.get("fields") or {}
            item = str(h.get("_id", "")).split("|")[0]
            title = (fields.get("meta_title") or [None])[0]
            snippet = ((h.get("highlight") or {}).get("text") or [None])[0]
            hits.append(SearchHit(query_id=query_id, backend=self.SERVICE, doi=None, title=title,
                                  url=f"https://archive.org/details/{item}" if item else None, license=None,
                                  snippet=snippet, notes=["snippet_only"]))
        return count, hits


class _Brave:
    SERVICE = "brave"
    KEYED_ONLY = True
    KEY_ENV = "BRAVE_API_KEY"
    BASE = "https://api.search.brave.com/res/v1/web/search"

    def build_search(self, phrase: str, req_id: str, use_key: bool = True) -> PlannedRequest:
        clean = phrase.replace('"', " ")
        url = self.BASE + "?" + urllib.parse.urlencode({"q": f'"{clean}"', "count": "5"}, quote_via=urllib.parse.quote)
        return PlannedRequest(id=req_id, service=self.SERVICE, method="GET", url=url, carries="user_text",
                              purpose="web search for this exact phrase (keyed)", text_sent=phrase,
                              headers={"X-Subscription-Token": "{BRAVE_API_KEY}"}, identity="BRAVE_API_KEY")

    def parse_search(self, body: bytes, query_id: str) -> tuple[int, list[SearchHit]]:
        # UNVERIFIED: the shape follows Brave's documented web search response; no recording exists without a key.
        data = json.loads(body.decode("utf-8"))
        results = (data.get("web") or {}).get("results", []) or []
        hits = [SearchHit(query_id=query_id, backend=self.SERVICE, doi=None, title=r.get("title"), url=r.get("url"),
                          license=None, fulltext_url=r.get("url"), snippet=r.get("description"),
                          notes=["web_page"]) for r in results if r.get("url")]
        return len(hits), hits


openlibrary = _OpenLibrary()
brave = _Brave()
