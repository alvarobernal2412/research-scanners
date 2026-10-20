import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_dois  # noqa: E402
from extract import Line  # noqa: E402

CHECK_PY = SCRIPTS / "check_dois.py"


def L(no, text, in_code=False, in_quote=False):
    return Line(no, text, in_code, in_quote)


class FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "m", headers or {}, io.BytesIO(b""))


def test_find_dois_strips_trailing_punctuation():
    lines = [
        L(1, "See (doi:10.1000/abc.def)."),
        L(2, "Link https://doi.org/10.1234/XY-1; and 10.1234/XY-1, again."),
        L(3, "10.5555/code", in_code=True),
        L(4, "quoted 10.9999/q.", in_quote=True),
    ]
    assert check_dois.find_dois(lines) == [
        (1, "10.1000/abc.def"),
        (2, "10.1234/XY-1"),
        (4, "10.9999/q"),
    ]


def test_resolve_404_is_not_found():
    def opener(req, timeout=None):
        raise http_error(404)

    r = check_dois.resolve("10.1000/x", opener=opener)
    assert r == {"doi": "10.1000/x", "status": "not_found", "title": None}


def test_resolve_ok_returns_title():
    body = json.dumps({"message": {"title": ["A Title", "B"]}}).encode()
    r = check_dois.resolve("10.1000/x", opener=lambda req, timeout=None: FakeResp(body))
    assert r["status"] == "ok" and r["title"] == "A Title"


def test_offline_makes_no_requests(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(check_dois.urllib.request, "urlopen", boom)
    f = tmp_path / "d.md"
    f.write_text("10.1000/x", encoding="utf-8")
    assert check_dois.main([str(f), "--offline"]) == 0
    assert json.loads(capsys.readouterr().out) == {"skipped": "offline"}


def test_request_url_contains_only_doi():
    seen = []

    def opener(req, timeout=None):
        seen.append((req, timeout))
        return FakeResp(b"{}")

    check_dois.resolve("10.1000/a b", opener=opener)
    req, timeout = seen[0]
    assert req.full_url == "https://api.crossref.org/works/10.1000/a%20b"
    assert req.data is None
    assert timeout == 15
    assert req.get_header("User-agent").startswith("llm-tropes-skill/1.0")
    assert {k.lower() for k, _ in req.header_items()} == {"user-agent"}


def test_resolve_429_honors_retry_after_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(check_dois, "sleep", sleeps.append)
    calls = []

    def opener(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise http_error(429, {"Retry-After": "99"})
        return FakeResp(b"{}")

    r = check_dois.resolve("10.1000/x", opener=opener)
    assert r["status"] == "ok"
    assert sleeps == [10]


def test_resolve_429_gives_error_after_two_retries(monkeypatch):
    monkeypatch.setattr(check_dois, "sleep", lambda s: None)
    calls = []

    def opener(req, timeout=None):
        calls.append(1)
        raise http_error(429, {"Retry-After": "1"})

    assert check_dois.resolve("10.1000/x", opener=opener)["status"] == "error"
    assert len(calls) == 3


def test_resolve_network_error_is_error():
    def opener(req, timeout=None):
        raise urllib.error.URLError("down")

    assert check_dois.resolve("10.1000/x", opener=opener)["status"] == "error"


def test_cli_offline_prints_skipped_and_makes_no_requests(tmp_path):
    f = tmp_path / "d.md"
    f.write_text("10.1000/x", encoding="utf-8")
    p = subprocess.run(
        [sys.executable, "-I", str(CHECK_PY), str(f), "--offline"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert p.returncode == 0
    assert json.loads(p.stdout) == {"skipped": "offline"}


def test_cli_missing_file():
    p = subprocess.run(
        [sys.executable, "-I", str(CHECK_PY), "nope.md"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert p.returncode == 2
    assert json.loads(p.stdout) == {"error": "file_not_found"}
