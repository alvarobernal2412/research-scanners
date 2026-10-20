"""Guarded HTTP transport: every request is checked against the approved plan first.

Redirects are never followed (a redirect to an unapproved host would be an exfiltration
path), only headers named in the plan (plus the fixed User-Agent) are sent, and secret
header values are read from the environment at send time and never logged.
"""
from __future__ import annotations

import gzip
import os
import sys
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval import Guard  # noqa: E402

USER_AGENT = "code-plagiarism-skill/1.0 (+report-only; sends only approved requests)"
MAX_RETRIES = 2
MAX_RETRY_AFTER = 30
DEFAULT_RETRY_AFTER = 1
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


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


def _secret_value(var: str) -> str:
    value = os.environ.get(var)
    if not value:
        raise NetError("secret_missing", f"environment variable {var} is not set")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise NetError("secret_invalid", f"environment variable {var} holds an unusable value")
    return value


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
        body = r.read()
    finally:
        close = getattr(r, "close", None)
        if close:
            close()
    if len(body) > MAX_RESPONSE_BYTES:
        raise NetError("network_error", "response too large")
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError, zlib.error):
            raise NetError("network_error", "invalid gzip body") from None
        headers.pop("content-encoding", None)
    return Response(int(status), headers, body)


def _once(opener, req) -> Response:
    call = getattr(opener, "open", None) or opener
    try:
        resp = _read_response(call(req, timeout=TIMEOUT_SECONDS))
    except urllib.error.HTTPError as exc:
        try:
            resp = _read_response(exc)
        except Exception:
            resp = Response(exc.code, {}, b"")
    except NetError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NetError("network_error", type(exc).__name__) from None
    if 300 <= resp.status < 400:
        raise NetError("redirect_blocked", f"HTTP {resp.status}; redirects are never followed")
    return resp


def send(method: str, url: str, guard: Guard, body: bytes | None = None, headers: dict | None = None,
         opener=None, sleep=time.sleep) -> Response:
    caller_headers = dict(headers) if headers else None
    guard.check(method, url, body, headers=caller_headers)          # before anything else
    final = {"User-Agent": USER_AGENT}
    for name, spec in guard.planned_headers(method, url, body).items():
        final[name] = _secret_value(spec["secret_from_env"]) if isinstance(spec, dict) else spec
    if opener is None:
        opener = build_default_opener()
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, method=method, headers=final)
        resp = _once(opener, req)
        if resp.status in (429, 503) and attempt < MAX_RETRIES:
            sleep(_retry_after(resp.headers))
            attempt += 1
            continue
        return resp
