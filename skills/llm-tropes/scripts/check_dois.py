"""Optional DOI validity check. The only networked part of the skill.

Privacy: the only thing ever sent is the DOI itself, in the URL path of a
Crossref works lookup. No document text, file name, or other content is sent.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import ExtractError, Line, extract  # noqa: E402

API = "https://api.crossref.org/works/"
USER_AGENT = "llm-tropes-skill/1.0 (doi-check; no document content is sent)"
TIMEOUT = 15
MAX_RETRIES = 2
MAX_RETRY_AFTER = 10
REQUEST_DELAY = 0.1
DISCLAIMER = (
    "Only DOIs were sent to api.crossref.org; no document text was sent. 'not_found' means "
    "Crossref does not know the DOI; it may still exist at another registrar such as DataCite."
)
_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_VALID = re.compile(r"10\.\d{4,9}/.")
_TRAIL = ".,;:)]}>'\""


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def find_dois(lines: list[Line]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for ln in lines:
        if ln.in_code:
            continue
        for m in _DOI.finditer(ln.text):
            doi = m.group(0).rstrip(_TRAIL)
            if not _VALID.match(doi):
                continue
            key = (ln.no, doi)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def _retry_after(err: urllib.error.HTTPError) -> float:
    try:
        value = float(err.headers.get("Retry-After", 1))
    except (TypeError, ValueError, AttributeError):
        value = 1
    return max(0.0, min(value, MAX_RETRY_AFTER))


def resolve(doi: str, opener=urllib.request.urlopen) -> dict:
    result = {"doi": doi, "status": "error", "title": None}
    url = API + urllib.parse.quote(doi, safe="/")
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with opener(req, timeout=TIMEOUT) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                result["status"] = "not_found"
                return result
            if e.code == 429 and attempt < MAX_RETRIES:
                sleep(_retry_after(e))
                continue
            return result
        except Exception:
            return result
        result["status"] = "ok"
        try:
            titles = json.loads(body).get("message", {}).get("title") or []
            if titles:
                result["title"] = titles[0]
        except Exception:
            pass
        return result
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check DOIs against Crossref (sends only DOIs).")
    ap.add_argument("file")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.offline:
        print(json.dumps({"skipped": "offline"}))
        return 0
    path = Path(args.file)
    if not path.is_file():
        print(json.dumps({"error": "file_not_found"}))
        return 2
    try:
        found = find_dois(extract(path))
    except ExtractError as e:
        print(json.dumps({"error": e.code}))
        return 2
    except Exception:
        print(json.dumps({"error": "unreadable_file"}))
        return 2
    cache: dict[str, dict] = {}
    results = []
    for line_no, doi in found:
        if doi not in cache:
            if cache:
                sleep(REQUEST_DELAY)
            cache[doi] = resolve(doi)
        r = cache[doi]
        results.append({"line": line_no, "doi": doi, "status": r["status"], "title": r["title"]})
    print(json.dumps({"file": str(path), "results": results, "disclaimer": DISCLAIMER},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
