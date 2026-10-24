"""Open-access full-text acquisition for cited works (strict tier: only public identifiers are sent).

Rounds: locate (OpenAlex work per DOI) -> zenodo (records named by OpenAlex) -> download (candidate URLs).
Every `plan_*` returns `PlannedRequest`s; every `apply_*` consumes a response body and updates a `SourceInfo`.
Downloaded text is cached under the user cache root, never inside the repo and never re-served.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval import PlannedRequest  # noqa: E402
from extract import ExtractError, Line, extract_bytes  # noqa: E402
from net import Response  # noqa: E402
from textnorm import GLUED_MAX_RATIO, glued_ratio, tokenize  # noqa: E402

MAX_CANDIDATES_PER_SOURCE = 2
ZENODO_TITLE_MIN_SIM = 0.5
OPENALEX_WORK = "https://api.openalex.org/works/doi:"
ZENODO_RECORD = "https://zenodo.org/api/records/"
_SUPPLEMENT = re.compile(r"appendix|supplement|supplementary|dataset|figure|table", re.I)
_VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
_ZENODO_DOI = re.compile(r"10\.5281/zenodo\.(\d+)", re.I)


@dataclass
class SourceInfo:
    key: str
    ref_n: int | None
    doi: str | None
    title: str | None
    is_oa: bool | None = None
    oa_status: str | None = None
    license: str | None = None
    version: str | None = None
    pmcid: str | None = None
    arxiv: str | None = None
    candidates: list[str] = field(default_factory=list)
    zenodo_ids: list[str] = field(default_factory=list)
    status: str = "pending"
    reason: str | None = None
    text_path: str | None = None
    url: str | None = None
    found_via: str = "strict:reference"
    cand_meta: dict = field(default_factory=dict)      # candidate url -> {license, version}
    tried: list[str] = field(default_factory=list)
    zenodo_done: list[str] = field(default_factory=list)


def service_for_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for suffix, service in (("europepmc.org", "europepmc"), ("ebi.ac.uk", "europepmc"), ("arxiv.org", "arxiv"),
                            ("zenodo.org", "zenodo"), ("openalex.org", "openalex"), ("core.ac.uk", "core")):
        if host == suffix or host.endswith("." + suffix):
            return service
    return "web"


def _openalex_url(doi: str) -> str:
    url = OPENALEX_WORK + urllib.parse.quote(doi, safe="/:()-._~")
    if os.environ.get("OPENALEX_API_KEY"):
        url += "?api_key={OPENALEX_API_KEY}"
    return url


def _identity() -> str | None:
    return "OPENALEX_API_KEY" if os.environ.get("OPENALEX_API_KEY") else None


def plan_locate(infos: list[SourceInfo]) -> list[PlannedRequest]:
    reqs = []
    for info in infos:
        if info.status != "pending" or not info.doi or info.is_oa is not None or info.candidates:
            continue
        reqs.append(PlannedRequest(id="", service="openalex", method="GET", url=_openalex_url(info.doi),
                                   carries="public_id", purpose="locate open-access copies of a cited work",
                                   subject=f"source:{info.key}", identity=_identity()))
        m = _ZENODO_DOI.fullmatch(info.doi)
        if m and m.group(1) not in info.zenodo_ids:
            info.zenodo_ids.append(m.group(1))
    return reqs


def _doi_from_url(url: str | None) -> str | None:
    m = re.search(r"10\.\d{4,9}/[^\s?#]+", url or "")
    return m.group().lower() if m else None


def apply_locate(info: SourceInfo, body: bytes) -> SourceInfo:
    try:
        work = json.loads(body.decode("utf-8"))
        if not isinstance(work, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        info.status, info.reason = "not_obtained", "backend_error"
        return info
    oa = work.get("open_access") or {}
    info.is_oa = bool(oa.get("is_oa"))
    info.oa_status = oa.get("oa_status")
    info.title = info.title or work.get("title")
    pmcid = (work.get("ids") or {}).get("pmcid")
    if pmcid:
        info.pmcid = pmcid.rsplit("/", 1)[-1]
    locations = [l for l in (work.get("locations") or []) if isinstance(l, dict)]
    if not info.pmcid:       # a PMC copy is often listed only as a location
        for loc in locations:
            m = re.search(r"europepmc\.org/(?:pmc/)?articles?/(?:PMC)?(\d+)|ncbi\.nlm\.nih\.gov/pmc/articles/(?:PMC)?(\d+)",
                          (loc.get("landing_page_url") or "") + " " + (loc.get("pdf_url") or ""))
            if m:
                info.pmcid = "PMC" + (m.group(1) or m.group(2))
                break
    best = work.get("best_oa_location") or {}
    cands: list[tuple[int, str, dict]] = []

    def add(rank: int, url: str | None, loc: dict):
        if url and url not in {c[1] for c in cands}:
            cands.append((rank, url, {"license": loc.get("license"), "version": loc.get("version")}))

    if info.pmcid and info.is_oa:
        add(0, f"https://www.ebi.ac.uk/europepmc/webservices/rest/{info.pmcid}/fullTextXML",
            {"license": best.get("license"), "version": best.get("version")})
    add(1, best.get("pdf_url"), best)
    for loc in sorted((l for l in locations if l.get("is_oa") and l.get("pdf_url")),
                      key=lambda l: _VERSION_RANK.get(l.get("version"), 3)):
        add(2 + _VERSION_RANK.get(loc.get("version"), 3), loc.get("pdf_url"), loc)
    for loc in locations:
        for field_ in ("landing_page_url", "pdf_url"):
            u = loc.get(field_) or ""
            m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", u)
            if m and not info.arxiv:
                info.arxiv = m.group(1)
            d = _doi_from_url(u)
            if d:
                z = _ZENODO_DOI.fullmatch(d)
                if z and z.group(1) not in info.zenodo_ids and loc.get("is_oa"):
                    info.zenodo_ids.append(z.group(1))
                a = re.fullmatch(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", d)
                if a and not info.arxiv:
                    info.arxiv = a.group(1)
    if info.arxiv:
        add(20, f"https://arxiv.org/pdf/{info.arxiv}", {"license": None, "version": None})
    cands.sort(key=lambda c: c[0])
    for _, url, meta in cands:
        if url not in info.candidates:
            info.candidates.append(url)
            info.cand_meta[url] = meta
    if not info.candidates and not info.zenodo_ids:
        info.status = "not_obtained"
        if not info.is_oa:
            info.reason = "paywalled"
        else:
            info.reason = "landing_page_only" if locations else "no_oa_location"
    return info


def plan_zenodo(infos: list[SourceInfo]) -> list[PlannedRequest]:
    reqs = []
    for info in infos:
        if info.status != "pending":
            continue
        for zid in info.zenodo_ids:
            if zid in info.zenodo_done:
                continue
            reqs.append(PlannedRequest(id="", service="zenodo", method="GET", url=f"{ZENODO_RECORD}{zid}",
                                       carries="public_id", purpose="inspect a Zenodo record listed as an open copy",
                                       subject=f"zenodo:{info.key}:{zid}"))
    return reqs


def _title_sim(a: str | None, b: str | None) -> float:
    ta = {t.key for t in tokenize([Line(1, a or "")])}
    tb = {t.key for t in tokenize([Line(1, b or "")])}
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def apply_zenodo(info: SourceInfo, body: bytes, record_id: str | None = None) -> SourceInfo:
    if record_id:
        info.zenodo_done.append(record_id)
    try:
        rec = json.loads(body.decode("utf-8"))
        meta = rec.get("metadata") or {}
        files = rec.get("files") or []
    except (ValueError, UnicodeDecodeError, AttributeError):
        info.reason = info.reason or "backend_error"
        return info
    rtype = (meta.get("resource_type") or {}).get("type")
    similar = _title_sim(info.title, meta.get("title")) >= ZENODO_TITLE_MIN_SIM or not info.title
    pdfs = [f for f in files if str(f.get("key", "")).lower().endswith(".pdf") and not _SUPPLEMENT.search(f.get("key", ""))]
    if rtype != "publication" or not similar or not pdfs:
        info.reason = "only_supplementary_files"
        return info
    lic = (meta.get("license") or {}).get("id")
    for f in pdfs[:1]:
        url = (f.get("links") or {}).get("self")
        if url and url not in info.candidates:
            info.candidates.append(url)
            info.cand_meta[url] = {"license": lic, "version": None}
    return info


def plan_download(infos: list[SourceInfo]) -> list[PlannedRequest]:
    reqs = []
    for info in infos:
        if info.status != "pending" or len(info.tried) >= MAX_CANDIDATES_PER_SOURCE:
            continue
        nxt = next((u for u in info.candidates if u not in info.tried), None)
        if nxt is None:
            continue
        reqs.append(PlannedRequest(id="", service=service_for_url(nxt), method="GET", url=nxt, carries="public_url",
                                   purpose="download an open-access full text for local comparison",
                                   subject=f"source:{info.key}"))
    return reqs


def settle(infos: list[SourceInfo]) -> None:
    """Mark sources with nothing left to try as not_obtained, with the most specific reason known."""
    for info in infos:
        if info.status != "pending" or (info.is_oa is None and not info.tried):
            continue          # not located yet: nothing is known about its copies
        untried = [u for u in info.candidates if u not in info.tried]
        zenodo_left = [z for z in info.zenodo_ids if z not in info.zenodo_done]
        exhausted = len(info.tried) >= MAX_CANDIDATES_PER_SOURCE
        if (not untried or exhausted) and not zenodo_left:
            info.status = "not_obtained"
            if info.reason is None:
                info.reason = "download_failed" if info.tried else ("no_oa_location" if info.is_oa else "not_resolved")


def text_from_response(resp: Response) -> tuple[list[Line] | None, str | None]:
    if resp.error or resp.status != 200:
        return None, "download_failed"
    head = resp.body[:4096].lstrip(b"\x00\xef\xbb\xbf \r\n\t").lower()
    if b"%pdf" in resp.body[:1024].lower():
        fmt = "pdf"
    elif head.startswith(b"<!doctype html") or b"<html" in head[:1024]:
        return None, "landing_page_only"
    elif b"<article" in head:
        fmt = "jats"
    else:
        return None, "download_failed"
    try:
        doc = extract_bytes(resp.body, fmt)
    except ExtractError as exc:
        return None, "no_text" if exc.code == "no_text_extracted" else "download_failed"
    if glued_ratio(tokenize(doc.lines)) > GLUED_MAX_RATIO:
        return None, "low_text_quality"
    return doc.lines, None


def apply_download(info: SourceInfo, resp: Response, store_dir: Path, url: str | None = None) -> SourceInfo:
    url = url or resp.final_url
    if url and url not in info.tried:
        info.tried.append(url)
    lines, reason = text_from_response(resp)
    if lines is None:
        info.reason = reason
        if resp.error in ("http_429", "http_5xx", "timeout", "network_error", "too_large"):
            info.reason = "download_failed"
        settle([info])
        return info
    meta = info.cand_meta.get(url or "", {})
    info.license = meta.get("license") or info.license
    info.version = meta.get("version") or info.version
    info.url = url
    info.status, info.reason = "obtained", None
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / (hashlib.sha256((url or info.key).encode("utf-8")).hexdigest() + ".json")
    path.write_text(json.dumps({
        "lines": [{"no": l.no, "text": l.text, "page": l.page, "heading_level": l.heading_level,
                   "in_quote": l.in_quote} for l in lines],
        "doi": info.doi, "url": url, "license": info.license, "version": info.version}, ensure_ascii=False),
        encoding="utf-8")
    info.text_path = str(path)
    return info


def load_source_lines(info: SourceInfo) -> list[Line]:
    data = json.loads(Path(info.text_path).read_text(encoding="utf-8"))
    return [Line(l["no"], l["text"], l.get("page"), l.get("heading_level"), bool(l.get("in_quote")))
            for l in data["lines"]]
