"""Record/replay helpers so tests never touch the network."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_KEY_PARAM = re.compile(r"(?<=[?&])key=[^&]*&?")


def _strip_key(url: str) -> str:
    out = _KEY_PARAM.sub("", url)
    return out.rstrip("?&") if out != url else url


def request_key(method: str, url: str, body) -> str:
    key = f"{method} {_strip_key(url)}"
    if body:
        key += " " + hashlib.sha256(body).hexdigest()
    return key


class _Resp:
    def __init__(self, url, status, headers, body):
        self.url, self.status, self.headers, self._body = url, status, dict(headers), body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _split(req):
    return req.get_method(), req.full_url, req.data


class ReplayOpener:
    """urlopen-compatible: answers only from recorded responses."""

    def __init__(self, responses_dir):
        self.dir = Path(responses_dir)
        self.index = json.loads((self.dir / "index.json").read_text(encoding="utf-8"))
        self.requests: list = []

    def open(self, req, timeout=None):
        method, url, body = _split(req)
        key = request_key(method, url, body)
        self.requests.append(key)
        entry = self.index.get(key)
        if entry is None:
            raise urllib.error.URLError("unrecorded")
        data = (self.dir / entry["file"]).read_bytes()
        status, headers = int(entry["status"]), entry.get("headers", {})
        if status >= 300:
            raise urllib.error.HTTPError(url, status, "recorded", headers, io.BytesIO(data))
        return _Resp(url, status, headers, data)

    __call__ = open


class RecordingOpener:
    """Passes through to a real opener and records decompressed bodies."""

    def __init__(self, real_opener, responses_dir):
        self.real = real_opener
        self.dir = Path(responses_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.requests: list = []
        path = self.dir / "index.json"
        self.index = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _record(self, key, status, headers, body):
        headers = {k: v for k, v in dict(headers).items()
                   if k.lower() not in ("content-encoding", "content-length", "set-cookie")}
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] + ".body"
        (self.dir / name).write_bytes(body)
        self.index[key] = {"file": name, "status": status, "headers": headers}
        (self.dir / "index.json").write_text(json.dumps(self.index, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _plain(headers, body):
        headers = dict(headers)
        if any(k.lower() == "content-encoding" and str(v).lower() == "gzip" for k, v in headers.items()):
            body = gzip.decompress(body)
        return headers, body

    def open(self, req, timeout=None):
        method, url, body = _split(req)
        key = request_key(method, url, body)
        self.requests.append(key)
        call = getattr(self.real, "open", None) or self.real
        try:
            r = call(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            headers, data = self._plain(dict(exc.headers or {}), exc.read())
            self._record(key, exc.code, headers, data)
            raise urllib.error.HTTPError(url, exc.code, exc.msg, headers, io.BytesIO(data)) from None
        status = getattr(r, "status", None) or r.getcode()
        headers, data = self._plain(dict(r.headers.items()), r.read())
        self._record(key, int(status), headers, data)
        return _Resp(url, int(status), {k: v for k, v in headers.items() if k.lower() != "content-encoding"}, data)

    __call__ = open


class ScanossReplayRunner:
    """Fake `runner(argv, env, cwd) -> (rc, stdout, stderr)` for the scanoss-py scan step."""

    def __init__(self, by_md5_path):
        self.by_md5 = json.loads(Path(by_md5_path).read_text(encoding="utf-8"))
        self.calls: list = []

    @staticmethod
    def _arg(argv, flag):
        return argv[argv.index(flag) + 1]

    def __call__(self, argv, env, cwd):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "cwd": str(cwd)})
        base = Path(cwd) if cwd else Path(".")
        try:
            wfp, out = self._arg(argv, "--wfp"), self._arg(argv, "-o")
        except (ValueError, IndexError):
            return 2, "", "missing --wfp or -o"
        wfp_path = Path(wfp) if Path(wfp).is_absolute() else base / wfp
        out_path = Path(out) if Path(out).is_absolute() else base / out
        result = {}
        for line in wfp_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("file="):
                md5, _size, name = line[5:].split(",", 2)
                result[name] = self.by_md5.get(md5) or [{"id": "none"}]
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0, "", ""
