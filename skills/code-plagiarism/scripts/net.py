"""Guarded HTTP transport: every request is checked against the approved plan first.

Redirects are never followed (a redirect to an unapproved host would be an exfiltration
path), only headers named in the plan (plus the fixed User-Agent) are sent, and secret
header values are read from the environment at send time and never logged.
"""
from __future__ import annotations

import http.client
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval import SECRET_VARS, TOKEN_RE, Guard  # noqa: E402

USER_AGENT = "code-plagiarism-skill/1.0 (+report-only; sends only approved requests)"
MAX_RETRIES = 2
MAX_RETRY_AFTER = 30
DEFAULT_RETRY_AFTER = 1
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_GZIP_MEMBERS = 64
DECODE_DEADLINE_SECONDS = 5
_clock = time.monotonic


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""


class NetError(Exception):
    """code in network_error|redirect_blocked|secret_missing|secret_invalid"""

    def __init__(self, code: str = "network_error", detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_default_opener():
    """Opener with only http(s) handlers (no file/ftp/data) and redirects disabled."""
    director = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.HTTPHandler(),
                    urllib.request.HTTPSHandler(), _NoRedirect(),
                    urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor(),
                    urllib.request.UnknownHandler()):
        director.add_handler(handler)
    return director


def _env(var: str):
    return os.environ.get(var)


def secret_values() -> list:
    """Values of the allowlisted secret variables that are set (for redaction in recordings)."""
    return [v for v in (_env(name) for name in sorted(SECRET_VARS)) if v]


def _secret_value(var: str) -> str:
    if var not in SECRET_VARS:
        raise NetError("secret_invalid", f"{var} is not an allowed secret variable")
    value = _env(var)
    if not value:
        raise NetError("secret_missing", f"environment variable {var} is not set")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise NetError("secret_invalid", f"environment variable {var} holds an unusable value")
    return value


def _wire_url(url: str) -> str:
    """Substitute the one {secret:NAME} token (if any); the placeholder form is what gets logged."""
    m = TOKEN_RE.search(url)
    if not m:
        return url
    return url.replace(m.group(0), urllib.parse.quote(_secret_value(m.group(1)), safe=""))


def gunzip_bounded(data: bytes) -> bytes:
    """gunzip with an output cap (MAX_RESPONSE_BYTES), a member cap and a wall-clock deadline.

    Walks the buffer through a memoryview, so a long run of tiny members never copies the whole
    remainder once per member; the member cap bounds the remaining per-member work."""
    cap = MAX_RESPONSE_BYTES
    deadline = _clock() + DECODE_DEADLINE_SECONDS
    view = memoryview(data)
    out, total, pos, members = [], 0, 0, 0
    try:
        while pos < len(view):
            members += 1
            if members > MAX_GZIP_MEMBERS:
                raise NetError("bad_encoding", "too many gzip members")
            if _clock() > deadline:
                raise NetError("decode_timeout", "decompression took too long")
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            chunk = d.decompress(view[pos:], cap + 1 - total)
            total += len(chunk)
            out.append(chunk)
            if total > cap or d.unconsumed_tail:
                raise NetError("response_too_large", "decompressed response exceeds the size cap")
            if not d.eof:
                raise NetError("network_error", "truncated gzip body")
            pos = len(view) - len(d.unused_data)
    except zlib.error:
        raise NetError("network_error", "invalid gzip body") from None
    except MemoryError:
        raise NetError("response_too_large", "decompressed response exceeds the size cap") from None
    return b"".join(out)


def _lower(headers) -> dict:
    try:
        return {str(k).lower(): str(v) for k, v in headers.items()}
    except AttributeError:
        return {}


def _retry_after(headers: dict) -> int:
    raw = headers.get("retry-after", "")
    try:
        secs = int(raw.strip())
    except ValueError:
        secs = DEFAULT_RETRY_AFTER
    return max(0, min(secs, MAX_RETRY_AFTER))


def _read_response(r) -> Response:
    status = getattr(r, "status", None)
    if status is None:
        status = r.getcode()
    headers = _lower(getattr(r, "headers", {}) or {})
    try:
        try:
            body = r.read(MAX_RESPONSE_BYTES + 1)
        except MemoryError:
            raise NetError("response_too_large", "response exceeds the size cap") from None
    finally:
        close = getattr(r, "close", None)
        if close:
            close()
    if len(body) > MAX_RESPONSE_BYTES:
        raise NetError("response_too_large", "response exceeds the size cap")
    if headers.get("content-encoding", "").lower() == "gzip":
        body = gunzip_bounded(body)
        headers.pop("content-encoding", None)
    return Response(int(status), headers, body)


def _once(opener, req) -> Response:
    call = getattr(opener, "open", None) or opener
    http_error = None
    try:
        raw = call(req, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        raw, http_error = exc, exc
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        raise NetError("network_error", type(exc).__name__) from None
    try:
        resp = _read_response(raw)
    except NetError:
        raise
    except (OSError, ValueError, MemoryError, http.client.HTTPException):
        if http_error is None:
            raise NetError("network_error", "unreadable response") from None
        resp = Response(http_error.code, {}, b"")
    if 300 <= resp.status < 400:
        raise NetError("redirect_blocked", f"HTTP {resp.status}; redirects are never followed")
    return resp


def send(method: str, url: str, guard: Guard, body: bytes | None = None, headers: dict | None = None,
         opener=None, sleep=time.sleep) -> Response:
    caller_headers = dict(headers) if headers else None
    guard.check(method, url, body, headers=caller_headers)          # before anything else
    try:
        final = {"User-Agent": USER_AGENT}
        for name, spec in guard.planned_headers(method, url, body).items():
            final[name] = _secret_value(spec["secret_from_env"]) if isinstance(spec, dict) else spec
        wire_url = _wire_url(url)
    except BaseException:
        guard.release(method, url, body)       # nothing was sent: do not log it, allow a retry later
        raise
    if opener is None:
        opener = build_default_opener()
    attempts = 0
    try:
        while True:
            attempts += 1
            req = urllib.request.Request(wire_url, data=body, method=method, headers=final)
            resp = _once(opener, req)
            if resp.status in (429, 503) and attempts <= MAX_RETRIES:
                sleep(_retry_after(resp.headers))
                continue
            guard.record(method, url, body, status=resp.status, attempts=attempts)
            return resp
    except BaseException as exc:
        code = exc.code if isinstance(exc, NetError) else type(exc).__name__
        guard.record(method, url, body, status=None, attempts=attempts, error=code)
        raise
