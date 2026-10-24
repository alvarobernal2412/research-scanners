"""Europe PMC open-tier backend: literal phrase search over open-access biomedical literature.

Backend contract: SERVICE, KEYED_ONLY, build_search(phrase, req_id, use_key) -> PlannedRequest,
parse_search(body, query_id) -> (hit_count, hits). The phrase is sent in double quotes, so inner double
quotes are escaped. Apostrophes matter: a curly one and a straight one do not match each other, so
`variants` returns both forms.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval import PlannedRequest  # noqa: E402
from hits import SearchHit  # noqa: E402

SERVICE = "europepmc"
KEYED_ONLY = False
KEY_ENV = None
BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"


def variants(phrase: str) -> list[str]:
    if "'" in phrase or "’" in phrase:
        straight = phrase.replace("’", "'")
        curly = phrase.replace("'", "’")
        return [straight, curly] if straight != curly else [straight]
    return [phrase]


def build_search(phrase: str, req_id: str, use_key: bool = False) -> PlannedRequest:
    escaped = phrase.replace("\\", "\\\\").replace('"', '\\"')
    url = BASE + "?" + urllib.parse.urlencode(
        {"query": f'"{escaped}"', "format": "json", "resultType": "core", "pageSize": "5"},
        quote_via=urllib.parse.quote)
    return PlannedRequest(id=req_id, service=SERVICE, method="GET", url=url, carries="user_text",
                          purpose="search Europe PMC for this exact phrase", text_sent=phrase)


def parse_search(body: bytes, query_id: str) -> tuple[int, list[SearchHit]]:
    data = json.loads(body.decode("utf-8"))
    count = int(data.get("hitCount") or 0)
    hits: list[SearchHit] = []
    for r in (data.get("resultList") or {}).get("result", []):
        pmcid = r.get("pmcid")
        oa = r.get("isOpenAccess") == "Y"
        lic = r.get("license")
        hits.append(SearchHit(
            query_id=query_id, backend=SERVICE, doi=(r.get("doi") or "").lower() or None, title=r.get("title"),
            url=f"https://europepmc.org/article/PMC/{pmcid}" if pmcid else None,
            license=lic.upper() if lic else None, pmcid=pmcid,
            fulltext_url=FULLTEXT.format(pmcid=pmcid) if pmcid and oa else None))
    return count, hits
