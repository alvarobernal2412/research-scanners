"""Record the backend responses that the tests replay (maintainer tool, run rarely).

    python -I record.py --list      print every URL that --record would fetch
    python -I record.py --record    fetch them (3 s apart, 6 s before CORE) into ./responses/

Only public identifiers and public CC BY sentences from the fixture works are sent. Nothing from a
user's document is ever in the list, and the T2 sentence is never used. Response file schema:
{"url", "method": "GET", "status", "headers" (lower-cased subset), "body_b64", "final_url"?}.
index.json maps canonical_url(url) to the file name.
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESP = HERE / "responses"
USER_AGENT = "text-plagiarism-skill/1.0 (report-only; fixture recording)"
KEEP_HEADERS = ("content-type", "retry-after", "x-ratelimit-remaining", "x-ratelimit-limit",
                "x-ratelimit-reset", "x-ratelimit-retry-after")
MAX_BYTES = 50_000_000

DOI_A = "10.1007/s12599-020-00641-4"        # Leno et al. 2020, CC BY 4.0
DOI_D = "10.2196/27499"                      # Maruster et al. 2021, JMIR, PMC8529480, CC BY 4.0
DOI_T1 = "10.1007/978-3-032-40795-5_31"      # paywalled Springer chapter (acceptance case T1)
PMCID_D = "PMC8529480"
PMCID_C = "PMC8684667"

# public CC BY sentences (fixtures spike 2026-10-20): A1 is a known miss, A2/A3 hits for OpenAlex windows
A1 = ("Unattended RPA bots, on the other hand, execute scripts without human involvement and do not "
      "take inputs during their execution.")
A2 = ("The events that do not share any data attributes and/or values or originate from uncommon "
      "sources most likely constitute noise.")
D1 = ("Process mining has an advantage over other mapping techniques as it uses factual observations "
      "retrieved from data rather than man-made process models.")
D3 = ("As a result, process mining further identifies the health care providers involved and the "
      "nature of their involvement in terms of specialties.")
C2_CURLY = ("We compute a set of metrics such as the minimum, median, maximum time of events, and mark "
            "them after the event’s name.")
C2_STRAIGHT = C2_CURLY.replace("’", "'")
REF_A = ("Leno, V., Polyvyanyy, A., Dumas, M., La Rosa, M., Maggi, F.M.: Robotic process mining: vision and "
         "challenges. Business & Information Systems Engineering 63(3), 301-314 (2021)")
BOOK_REF = "Tukey, J. W. (1977). Exploratory Data Analysis. Addison-Wesley."
FAMOUS = "it was the best of times, it was the worst of times"


def canonical_url(url: str) -> str:
    """Scheme/host lower-cased, query params sorted and re-encoded, fragment dropped."""
    p = urllib.parse.urlsplit(url)
    pairs = sorted(urllib.parse.parse_qsl(p.query, keep_blank_values=True))
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, query, ""))


def window(sentence: str, start: int = 0, n: int = 12) -> str:
    words = sentence.rstrip(".").replace(",", "").replace("|", "").split()
    return " ".join(words[start:start + n])


def q(url: str, **params) -> str:
    return url + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def static_urls() -> list[tuple[str, str]]:
    """(label, url) for everything that does not depend on an earlier response."""
    u: list[tuple[str, str]] = []
    for label, doi in (("oa-work-A", DOI_A), ("oa-work-D", DOI_D), ("oa-work-T1", DOI_T1)):
        u.append((label, f"https://api.openalex.org/works/doi:{doi}"))
    u.append(("zenodo-21373046", "https://zenodo.org/api/records/21373046"))
    u.append(("zenodo-21373047", "https://zenodo.org/api/records/21373047"))
    u.append(("epmc-fulltext-D", f"https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID_D}/fullTextXML"))
    epmc = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    for label, s in (("epmc-search-D1", D1), ("epmc-search-D3", D3), ("epmc-search-A1", A1),
                     ("epmc-search-C2-straight", C2_STRAIGHT), ("epmc-search-C2-curly", C2_CURLY)):
        u.append((label, q(epmc, query=f'"{s}"', format="json", resultType="core", pageSize="5")))
    oa = "https://api.openalex.org/works"
    for label, s in (("oa-fulltext-A2-w12", window(A2)), ("oa-fulltext-A1-w12", window(A1)),
                     ("oa-fulltext-D1-w12", window(D1))):
        u.append((label, q(oa, filter=f'fulltext.search:"{s}"', **{"per-page": "5"})))
    core = "https://api.core.ac.uk/v3/search/works/"
    u.append(("core-A2", q(core, q=f'("{A2}")', limit="5")))
    u.append(("core-D1", q(core, q=f'("{D1}")', limit="5")))
    u.append(("openlibrary-famous", q("https://openlibrary.org/search/inside.json", q=f'"{FAMOUS}"')))
    u.append(("crossref-book", q("https://api.crossref.org/works", **{"query.bibliographic": BOOK_REF, "rows": "2"})))
    return u


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    final = None
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(MAX_BYTES + 1)
            status, headers, final = r.status, r.headers, r.geturl()
    except urllib.error.HTTPError as e:
        body, status, headers = e.read(MAX_BYTES + 1), e.code, e.headers
    if len(body) > MAX_BYTES:
        raise SystemExit(f"too large: {url}")
    rec = {"url": url, "method": "GET", "status": status,
           "headers": {k: v for k, v in ((k.lower(), v) for k, v in headers.items()) if k in KEEP_HEADERS},
           "body_b64": base64.b64encode(body).decode("ascii")}
    if final and canonical_url(final) != canonical_url(url):
        rec["final_url"] = final
    return rec


def derived_urls(read) -> list[tuple[str, str]]:
    """URLs that depend on an earlier response (the open PDF of work A) plus the Crossref query for its reference string."""
    out = []
    work = json.loads(read("oa-work-A"))
    loc = work.get("best_oa_location") or {}
    if loc.get("pdf_url"):
        out.append(("pdf-A", loc["pdf_url"]))
    out.append(("crossref-A", q("https://api.crossref.org/works", **{"query.bibliographic": REF_A, "rows": "2"})))
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--list" in argv:
        for label, url in static_urls():
            print(f"{label}\t{url}")
        print("pdf-A\t(the best_oa_location.pdf_url named in the oa-work-A response)")
        print("crossref-A\t(Crossref bibliographic query for the reference string of work A)")
        return 0
    if "--record" not in argv:
        print(__doc__)
        return 2
    RESP.mkdir(exist_ok=True)
    index_path = RESP / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    labels: dict[str, str] = {}

    def read(label):
        return base64.b64decode(json.loads((RESP / labels[label]).read_text(encoding="utf-8"))["body_b64"])

    def run(items):
        for label, url in items:
            time.sleep(6 if label.startswith("core") else 3)
            rec = fetch(url)
            name = f"{label}.json"
            (RESP / name).write_text(json.dumps(rec, indent=1), encoding="utf-8")
            index[canonical_url(url)] = name
            labels[label] = name
            print(f"{rec['status']}\t{label}", flush=True)

    run(static_urls())
    run(derived_urls(read))
    index_path.write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
