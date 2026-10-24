# Backends

Measured facts from the backend spike (2026-10-20) and the fixtures spike (2026-10-20), plus what this repository's recorded responses show. Where the spikes disagree with earlier assumptions, the spikes win.

| Service | Used for | What it searches | Notes |
|---|---|---|---|
| Crossref | resolving reference strings to DOIs | bibliographic metadata, not text | accepted when the top score is at least 70 and leads the second by at least 20; no text search |
| OpenAlex | locating open copies by DOI; open-tier phrase search | `fulltext.search` is stemmed and covers about 17% of works | keyless use shares a small daily budget ($0.001 per search); 429 is honoured and the open tier stops below 10 remaining requests and records the rest as `budget_exhausted`; in testing about 40% of true sentences were never found |
| Europe PMC | open full text (JATS XML); open-tier phrase search | literal phrase in open-access biomedical literature | a curly and a straight apostrophe do not match each other, so both are queried; full text is the `fullTextXML` of the PMC id |
| Zenodo | records named as open copies by OpenAlex | record metadata and files | a record holding only an appendix, figures or data is `only_supplementary_files`, not the paper |
| arXiv | PDF of an arXiv id | n/a | 3 s between requests |
| CORE | opt-in open-tier phrase search | parenthesised phrase, trailing slash required | unreliable in the spike (500s, 429s, no verified body-text hit); not a default backend, marked unverified, not used in acceptance tests |
| Open Library | opt-in open-tier phrase search | inside digitised books | returns highlighted snippets only: hits are unverified candidates, never findings |
| Brave | opt-in open-tier web search | the open web | keyed only (`X-Subscription-Token`); the response shape in the tests is synthetic because no key was available; result pages are fetched and verified locally |

Not used: DuckDuckGo and Bing scraping (against their terms and brittle), Semantic Scholar (429 in the spike, unverified), Unpaywall.

## Verification

A search hit counts only when the locally recomputed overlap finds at least 8 consecutive identical words (or 80% of the query window) of the sentence in the hit's text; unrelated documents reached at most 6 words in the spike. A verified source is then compared against the whole document, so the full copied passage is reported, not only the queried sentence. A sentence that 5 or more search results contain, or that 3 or more distinct sources match, is reported as boilerplate instead of a finding.

## Fetching

Plain HTTP fetches worked for Springer and BMC PDFs and Europe PMC `fullTextXML`; MDPI, PeerJ, ScienceDirect, JMIR PDF and the Europe PMC PDF render returned 403, 202 or empty bodies, and publishers now sometimes answer with a bot challenge page. Such failures are coverage notes (`download_failed`, `landing_page_only`), never silent.

## Rate limits (seconds between requests per service)

crossref 0.34, openalex 0.2, zenodo 0.5, arxiv 3.0, europepmc 0.2, core 6.0, openlibrary 1.0, brave 1.0, other hosts 1.0. `Retry-After` and CORE's `x-ratelimit-retry-after` are honoured up to 60 s with at most 2 retries; timeout 20 s; downloads are capped at 50 MB.
