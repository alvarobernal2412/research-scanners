"""Gated HTTP transport with a cache, per-service rate limits, retries and a request log.

`send` first asks the `Gate` (it raises `UnapprovedRequest` for anything that is not in the approved plan),
then renders `{ENV_NAME}` placeholders from the environment, and never raises for HTTP or network failures:
those come back as `Response.error`. Redirects are followed only for GET requests that carry no user text.
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval import PLACEHOLDER, SECRET_ENVS, Gate, PlannedRequest, UnapprovedRequest  # noqa: E402,F401

USER_AGENT = "text-plagiarism-skill/1.0 (report-only)"
MIN_INTERVAL = {"crossref": 0.34, "openalex": 0.2, "zenodo": 0.5, "arxiv": 3.0, "europepmc": 0.2,
                "core": 6.0, "openlibrary": 1.0, "brave": 1.0, "web": 1.0}
DEFAULT_INTERVAL = 1.0
MAX_RETRIES = 2
MAX_RETRY_AFTER = 60
TIMEOUT = 20
MAX_DOWNLOAD_BYTES = 50_000_000
MAX_REDIRECTS = 5
KEEP_HEADERS = ("content-type", "retry-after", "x-ratelimit-remaining", "x-ratelimit-limit", "x-ratelimit-reset",
                "x-ratelimit-retry-after", "content-length")

_LAST: dict[str, float] = {}


def reset_rate_limits() -> None:
    _LAST.clear()


def user_agent(service: str) -> str:
    mailto = os.environ.get("TEXT_PLAGIARISM_MAILTO", "").strip()
    if service == "crossref" and mailto:
        return f"{USER_AGENT} (mailto:{mailto})"
    return USER_AGENT


def cache_root() -> Path:
    override = os.environ.get("TEXT_PLAGIARISM_CACHE")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "text-plagiarism"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "text-plagiarism"


def canonical_url(url: str) -> str:
    """Scheme/host lower-cased, query params sorted and re-encoded, fragment dropped."""
    p = urllib.parse.urlsplit(url)
    pairs = sorted(urllib.parse.parse_qsl(p.query, keep_blank_values=True))
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, query, ""))


@dataclass
class Response:
    request_id: str
    status: int | None
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    final_url: str | None = None
    from_cache: bool = False


def _cache_key(req: PlannedRequest) -> str:
    raw = f"{req.method}\n{req.url}\n{req.body or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Cache:
    """Downloaded content cache under the user cache root. Only 200 responses are stored."""

    def __init__(self, root: Path):
        self.root = Path(root) / "http"

    def _path(self, req: PlannedRequest) -> Path:
        key = _cache_key(req)
        return self.root / key[:2] / f"{key}.json"

    def get(self, req: PlannedRequest) -> Response | None:
        p = self._path(req)
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            return Response(req.id, rec["status"], base64.b64decode(rec["body_b64"]), rec.get("headers", {}), None,
                            rec.get("final_url"), True)
        except (OSError, ValueError, KeyError):
            return None

    def put(self, req: PlannedRequest, resp: Response) -> None:
        if resp.status != 200 or resp.error:
            return
        p = self._path(req)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"status": 200, "headers": resp.headers, "final_url": resp.final_url,
                                     "body_b64": base64.b64encode(resp.body).decode("ascii")}), encoding="utf-8")
        except OSError:
            pass


class RequestLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, req: PlannedRequest, resp: Response, tier: str, round_name: str) -> None:
        entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "request_id": req.id,
                 "service": req.service, "tier": tier, "round": round_name, "carries": req.carries,
                 "url": req.url, "text_sent": req.text_sent, "identity": req.identity, "status": resp.status,
                 "error": resp.error, "final_url": resp.final_url, "from_cache": resp.from_cache}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- opener

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_default_opener():
    director = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                    _NoRedirect(), urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor(),
                    urllib.request.UnknownHandler()):
        director.add_handler(handler)
    return director


class _ReplayResponse(io.BytesIO):
    def __init__(self, url, status, headers, body):
        super().__init__(body)
        self.url, self.status, self.headers = url, status, headers

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status


def _strip_secret_params(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k != "api_key"]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote), ""))


def replay_opener(responses_dir: Path):
    """Opener serving recorded responses (tests); an unrecorded URL raises URLError('not recorded')."""
    root = Path(responses_dir)
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))

    def opener(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        name = index.get(canonical_url(_strip_secret_params(url)))
        if name is None:
            raise urllib.error.URLError("not recorded")
        rec = json.loads((root / name).read_text(encoding="utf-8"))
        return _ReplayResponse(rec.get("final_url") or url, rec["status"], dict(rec.get("headers", {})),
                               base64.b64decode(rec["body_b64"]))

    return opener


# ---------------------------------------------------------------- helpers

def _lower(headers) -> dict[str, str]:
    try:
        return {str(k).lower(): str(v) for k, v in headers.items()}
    except AttributeError:
        return {}


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw:
        raw = raw.strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return float(raw)
        try:
            when = email.utils.parsedate_to_datetime(raw)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            pass
    iso = headers.get("x-ratelimit-retry-after")
    if iso:
        try:
            when = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except ValueError:
            pass
    return None


def _gunzip(data: bytes) -> bytes | None:
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(data, MAX_DOWNLOAD_BYTES + 1)
    except zlib.error:
        return None
    return out if len(out) <= MAX_DOWNLOAD_BYTES else None


def _render(text: str | None, quote: bool) -> tuple[str | None, str | None]:
    """Replace {ENV_NAME} placeholders; returns (rendered, missing_name)."""
    if text is None:
        return None, None
    missing = None

    def sub(m):
        nonlocal missing
        name = m.group(1)
        value = os.environ.get(name, "") if name in SECRET_ENVS else ""
        if not value or any(ord(c) < 32 for c in value):
            missing = name
            return m.group(0)
        return urllib.parse.quote(value, safe="") if quote else value

    return PLACEHOLDER.sub(sub, text), missing


def _once(opener, url: str, method: str, headers: dict, body: bytes | None):
    """One HTTP exchange. Returns (status, headers, body, error, url the response came from)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    call = getattr(opener, "open", None) or opener
    err = None
    try:
        raw = call(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        raw, err = exc, exc
    except TimeoutError:
        return None, {}, b"", "timeout", None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError) or "timed out" in str(exc.reason).lower():
            return None, {}, b"", "timeout", None
        return None, {}, b"", "network_error", None
    except (OSError, ValueError, http.client.HTTPException):
        return None, {}, b"", "network_error", None
    try:
        status = getattr(raw, "status", None) or raw.getcode()
        hdrs = _lower(getattr(raw, "headers", {}) or {})
        got = raw.geturl() if hasattr(raw, "geturl") else None
        data = raw.read(MAX_DOWNLOAD_BYTES + 1)
    except TimeoutError:
        return None, {}, b"", "timeout", None
    except (OSError, http.client.HTTPException):
        if err is not None:
            status, hdrs, data, got = err.code, _lower(err.headers or {}), b"", None
        else:
            return None, {}, b"", "network_error", None
    finally:
        close = getattr(raw, "close", None)
        if close:
            close()
    if len(data) > MAX_DOWNLOAD_BYTES:
        return int(status), hdrs, b"", "too_large", None
    if hdrs.get("content-encoding", "").lower() == "gzip":
        unzipped = _gunzip(data)
        if unzipped is None:
            return int(status), hdrs, b"", "too_large", None
        data = unzipped
    return int(status), hdrs, data, None, got


def _error_for(status: int) -> str | None:
    if status == 429:
        return "http_429"
    if status >= 500:
        return "http_5xx"
    if status >= 400:
        return "http_4xx"
    return None


def send(req: PlannedRequest, gate: Gate, *, tier: str, round_name: str, opener=None, cache: Cache | None = None,
         log: RequestLog | None = None, sleep=time.sleep) -> Response:
    gate.check(req)    # before anything else; raises UnapprovedRequest
    resp = _send_approved(req, opener=opener, cache=cache, sleep=sleep)
    if log is not None:
        log.append(req, resp, tier, round_name)
    return resp


def _send_approved(req: PlannedRequest, *, opener, cache: Cache | None, sleep) -> Response:
    url, missing = _render(req.url, quote=True)
    headers_t = {k: v for k, v in req.headers.items()}
    wire_headers: dict[str, str] = {}
    for k, v in headers_t.items():
        rendered, miss = _render(v, quote=False)
        missing = missing or miss
        wire_headers[k] = rendered or ""
    body_s, miss = _render(req.body, quote=False)
    missing = missing or miss
    if missing:
        return Response(req.id, None, b"", {}, "missing_key")
    if cache is not None:
        hit = cache.get(req)
        if hit is not None:
            return hit
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return Response(req.id, None, b"", {}, "network_error")
    wire_headers.setdefault("User-Agent", user_agent(req.service))
    body = body_s.encode("utf-8") if body_s is not None else None
    if opener is None:
        opener = build_default_opener()
    interval = MIN_INTERVAL.get(req.service, DEFAULT_INTERVAL)
    follow = req.method == "GET" and req.carries != "user_text"
    current = url
    final_url = None
    retries = 0
    hops = 0
    while True:
        last = _LAST.get(req.service)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                sleep(wait)
        _LAST[req.service] = time.monotonic()
        status, hdrs, data, error, got_url = _once(opener, current, req.method, wire_headers, body)
        _LAST[req.service] = time.monotonic()
        if error:
            return Response(req.id, status, b"", {k: v for k, v in hdrs.items() if k in KEEP_HEADERS}, error, final_url)
        assert status is not None
        if status == 429 and retries < MAX_RETRIES:
            wait_s = _retry_after_seconds(hdrs)
            if wait_s is not None and wait_s <= MAX_RETRY_AFTER:
                retries += 1
                sleep(wait_s)
                continue
        if status in (301, 302, 303, 307, 308) and hdrs.get("location"):
            if not follow:
                return Response(req.id, status, b"", {}, "redirect_blocked", final_url)
            hops += 1
            if hops > MAX_REDIRECTS:
                return Response(req.id, status, b"", {}, "network_error", final_url)
            current = urllib.parse.urljoin(current, hdrs["location"])
            final_url = current
            continue
        if final_url is None and got_url and got_url != current:
            final_url = got_url   # the opener itself followed a redirect (recorded responses do)
        resp = Response(req.id, status, data, {k: v for k, v in hdrs.items() if k in KEEP_HEADERS},
                        _error_for(status), final_url)
        if cache is not None:
            cache.put(req, resp)
        return resp
