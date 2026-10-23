"""Look references up in Crossref, DataCite, OpenAlex and the DOI handle service.

Privacy: what is sent is bibliographic metadata of the cited works (DOI, arXiv id, title, first
authors, year) in URL parameters. Never body text of the paper. Every request is recorded.
Set CROSSREF_MAILTO to join the polite pools of Crossref and OpenAlex.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import refs_compare as rc  # noqa: E402

TIMEOUT = 20
MAX_RETRIES = 2
MAX_RETRY_AFTER = 10
REQUEST_DELAY = 0.1
CROSSREF = "https://api.crossref.org/works"
DATACITE = "https://api.datacite.org/dois/"
OPENALEX = "https://api.openalex.org/works"
HANDLE = "https://doi.org/api/handles/"
ARXIV_DOI = "10.48550/arXiv."
DATACITE_FIRST = ("10.48550/", "10.5281/", "10.6084/", "10.17605/")


def user_agent() -> str:
    mail = os.environ.get("CROSSREF_MAILTO")
    base = "reference-check-skill/1.0 (checks cited works exist; sends only reference metadata"
    return base + (f"; mailto:{mail})" if mail else ")")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retry_after(err: urllib.error.HTTPError) -> float:
    try:
        value = float(err.headers.get("Retry-After", 1))
    except (TypeError, ValueError, AttributeError):
        value = 1
    return max(0.0, min(value, MAX_RETRY_AFTER))


class Session:
    """Holds the opener, request log and response cache for one run."""

    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen
        self.log: list[dict] = []
        self.cache: dict[str, tuple] = {}
        self.errors: list[str] = []
        self._first = True

    def get(self, url: str, service: str):
        """Returns (json|None, status) with status ok|not_found|error. Cached by URL."""
        if url in self.cache:
            return self.cache[url]
        if not self._first:
            sleep(REQUEST_DELAY)
        self._first = False
        self.log.append({"service": service, "url": url})
        result = (None, "error")
        for attempt in range(MAX_RETRIES + 1):
            req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": "application/json"})
            try:
                with self.opener(req, timeout=TIMEOUT) as resp:
                    body = resp.read()
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    result = (_safe_json(getattr(e, "fp", None)), "not_found")
                    break
                if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                    sleep(_retry_after(e))
                    continue
                self.errors.append(f"{service}: HTTP {e.code}")
                break
            except Exception as e:  # network down, timeout, TLS
                self.errors.append(f"{service}: {type(e).__name__}")
                break
            try:
                result = (json.loads(body), "ok")
            except ValueError:
                self.errors.append(f"{service}: unreadable response")
            break
        self.cache[url] = result
        return result


def _safe_json(fp):
    try:
        return json.loads(fp.read()) if fp else None
    except Exception:
        return None


# ---- record normalisers ------------------------------------------------------------------

def _year_from_parts(msg: dict) -> int | None:
    for key in ("issued", "published-print", "published-online", "published"):
        parts = (msg.get(key) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None


def parse_crossref(msg: dict) -> dict:
    return {
        "doi": msg.get("DOI"),
        "title": (msg.get("title") or [None])[0],
        "authors": [a.get("family") or a.get("name") or "" for a in msg.get("author", []) or []],
        "year": _year_from_parts(msg),
        "venues": (msg.get("container-title") or []) + (msg.get("short-container-title") or []),
        "type": msg.get("type"), "url": msg.get("URL"), "source": "crossref",
    }


def parse_datacite(data: dict) -> dict:
    a = data.get("attributes", {})
    authors = []
    for c in a.get("creators", []) or []:
        fam = c.get("familyName")
        if not fam:
            name = c.get("name", "")
            fam = name.split(",")[0] if "," in name else (name.split()[-1] if name else "")
        authors.append(fam)
    venues = []
    cont = a.get("container") or {}
    if cont.get("title"):
        venues.append(cont["title"])
    if a.get("publisher"):
        venues.append(a["publisher"] if isinstance(a["publisher"], str) else (a["publisher"] or {}).get("name", ""))
    titles = a.get("titles") or [{}]
    return {
        "doi": a.get("doi"), "title": titles[0].get("title"), "authors": authors,
        "year": a.get("publicationYear"), "venues": [v for v in venues if v],
        "type": (a.get("types") or {}).get("resourceTypeGeneral"), "url": a.get("url"), "source": "datacite",
    }


def parse_openalex(w: dict) -> dict:
    authors = []
    for au in w.get("authorships", []) or []:
        name = (au.get("author") or {}).get("display_name") or ""
        authors.append(name.split()[-1] if name else "")
    src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
    doi = w.get("doi") or ""
    return {
        "doi": re.sub(r"^https?://doi\.org/", "", doi) or None, "title": w.get("title") or w.get("display_name"),
        "authors": authors, "year": w.get("publication_year"), "venues": [src] if src else [],
        "type": w.get("type"), "url": w.get("id"), "source": "openalex",
    }


# ---- lookups -----------------------------------------------------------------------------

def _mailto_param() -> str:
    mail = os.environ.get("CROSSREF_MAILTO")
    return f"&mailto={urllib.parse.quote(mail)}" if mail else ""


def by_doi(session: Session, doi: str):
    """Returns (record|None, status) with status found|not_found|error."""
    qdoi = urllib.parse.quote(doi, safe="/")
    order = ["datacite", "crossref"] if doi.lower().startswith(DATACITE_FIRST) else ["crossref", "datacite"]
    errored = False
    for svc in order:
        if svc == "crossref":
            doc, st = session.get(f"{CROSSREF}/{qdoi}" + ("?" + _mailto_param()[1:] if _mailto_param() else ""),
                                  "api.crossref.org")
            if st == "ok" and doc and doc.get("message"):
                return parse_crossref(doc["message"]), "found"
        else:
            doc, st = session.get(DATACITE + qdoi, "api.datacite.org")
            if st == "ok" and doc and doc.get("data"):
                return parse_datacite(doc["data"]), "found"
        if st == "error":
            errored = True
    return None, ("error" if errored else "not_found")


def handle_status(session: Session, doi: str) -> str:
    """registered | not_registered | error, from the DOI handle service."""
    doc, st = session.get(HANDLE + urllib.parse.quote(doi, safe="/"), "doi.org")
    if st == "not_found":
        return "not_registered"
    if st == "ok" and doc:
        return "registered" if doc.get("responseCode") == 1 else "not_registered" if doc.get("responseCode") == 100 else "error"
    return "error"


def search_title(session: Session, ref: dict):
    """Search by title. Returns (record|None, status found|not_found|error)."""
    title = ref.get("title")
    raw_mode = not title
    errored = False
    records: list[dict] = []
    if title:
        q = " ".join([title] + list(ref.get("authors") or [])[:2] + ([str(ref["year"])] if ref.get("year") else []))
    else:
        q = ref.get("raw", "")[:300]
    if not q.strip():
        return None, "not_found"
    url = (f"{CROSSREF}?query.bibliographic={urllib.parse.quote(q)}&rows=3&select="
           "DOI,title,author,issued,published-print,published-online,container-title,short-container-title,type,URL"
           + _mailto_param())
    doc, st = session.get(url, "api.crossref.org")
    if st == "ok" and doc:
        records += [parse_crossref(m) for m in (doc.get("message", {}).get("items") or [])]
    elif st == "error":
        errored = True
    hit = _pick(ref, records, raw_mode)
    if hit:
        return hit, "found"
    if title:
        url = (f"{OPENALEX}?search={urllib.parse.quote(title)}&per-page=3&select="
               "id,doi,title,display_name,publication_year,authorships,primary_location,type" + _mailto_param())
        doc, st = session.get(url, "api.openalex.org")
        if st == "ok" and doc:
            hit = _pick(ref, [parse_openalex(w) for w in doc.get("results", [])], False)
            if hit:
                return hit, "found"
        elif st == "error":
            errored = True
    return None, ("error" if errored else "not_found")


def _pick(ref: dict, records: list[dict], raw_mode: bool):
    if not raw_mode:
        return rc.best_candidate(ref, records)
    raw = rc.norm(ref.get("raw"))
    for r in records:
        t = rc.norm(r.get("title"))
        if len(t) >= 20 and t in raw:
            if ref.get("year") and r.get("year") and abs(ref["year"] - r["year"]) > 1:
                continue
            return r
    return None


def resolve(session: Session, ref: dict) -> dict:
    """Find the record for one reference.

    status: found | not_found | doi_not_registered | doi_registered_unindexed | lookup_failed
    via: doi | arxiv | title_search
    """
    doi, via = ref.get("doi"), "doi"
    if not doi and ref.get("arxiv"):
        doi, via = ARXIV_DOI + ref["arxiv"], "arxiv"
    if doi:
        rec, st = by_doi(session, doi)
        if st == "found":
            return {"status": "found", "record": rec, "via": via}
        if st == "error":
            return {"status": "lookup_failed", "record": None, "via": via}
        h = handle_status(session, doi)
        if h == "error":
            return {"status": "lookup_failed", "record": None, "via": via}
        out = {"status": "doi_not_registered" if h == "not_registered" else "doi_registered_unindexed",
               "record": None, "via": via}
        if h == "not_registered" and ref.get("title"):
            alt, ast = search_title(session, ref)
            if alt:
                out["alternative_record"] = alt
        return out
    rec, st = search_title(session, ref)
    if st == "found":
        return {"status": "found", "record": rec, "via": "title_search"}
    return {"status": "lookup_failed" if st == "error" else "not_found", "record": None, "via": "title_search"}
