# Report format

`report.py` prints one JSON object with keys in this order: `coverage`, `summary`, `findings`, `informational`, `boilerplate`, `unverified_candidates`, `disclosure`, `disclaimer`.

## coverage

- `status`: `findings`, `no_overlap_found_in_checked_sources` or `insufficient_coverage`. The word "clean" and any score or verdict never appear. `no_overlap_found_in_checked_sources` requires no findings, enough checked sources (at least 3 full texts and at least half of the cited and supplied works, or at least 10 open-tier sentences queried with a backend that had no errors), no glued-word warning, no failed service request in the resolve, locate, zenodo, search or verify rounds, and no corpus item that failed to load. Otherwise the status is `insufficient_coverage`.
- `status_reasons`: why coverage was insufficient.
- `tiers_used`: `strict`, `open` (empty when only `prepare.py` ran).
- `extraction`: `fmt`, `pages`, `warnings` (`glued_words`, `references_not_found`).
- `sections`: `id`, `heading`, `kind`, `checked_strict`, `checked_open`, `excluded_reason`.
- `references`: `total`, `resolved`, `unresolved` (`n`, `text`, `resolution`), `full_text_obtained` (`n`, `doi`, `title`, `license`, `version`, `url`), `full_text_not_obtained` (`n`, `doi`, `title`, `reason`), `checked_of_total`.
  Resolutions: `doi_in_text`, `arxiv_in_text`, `crossref_accepted`, `crossref_ambiguous`, `crossref_low_score`, `not_sent_unpublished`, `backend_error`, `not_attempted`.
  Reasons: `paywalled`, `no_oa_location`, `only_supplementary_files`, `landing_page_only`, `download_failed`, `low_text_quality`, `no_text`, `not_resolved`, `backend_error`, `budget_exhausted`, `not_attempted`.
- `open`: `ran`, `sections_selected`, `sentences_eligible`, `sentences_queried`, `excluded_by_filter` (reason to count), `not_queried_over_cap` (section to count), `queries_failed`.
- `backends`: per service `sent`, `ok`, `cached`, `errors` (code to count).
- `corpus`: `provided` (`id`, `name`, `kind`, `status`, `reason`, `words`, `relation`), `compared`, `not_loaded`.
- `limits`: constant strings, plus those of the backends actually used.

## findings, informational, boilerplate

Each finding has `id`, `passage`, `location` (`section`, `line_start`, `line_end`, `page`), `source` (`doi`, `title`, `url`, `license`, `version`, `oa_status`, `found_via`: `strict:reference`, `user_corpus` or `open:<backend>`), `overlap_words`, `longest_run`, `match_type` (`verbatim` or `near_verbatim`), `attribution`, `severity`, `confidence`, `reference_entry`, `cites_other_source`, and `kind: "self_overlap"` for the user's own earlier work.

| Attribution | Severity |
|---|---|
| `quoted_cited` | `info` (listed under `informational`) |
| `cited_not_quoted` | `light` |
| `quoted_uncited` | `light` |
| `unattributed` | `flag` |

`confidence` is `high` when the longest exact run is at least 20 words, else `medium`. `boilerplate` entries are passages many independent sources contain; entries derived from search hit counts carry `search_hit_count`. `unverified_candidates` are search hits whose text could not be checked (snippet only, or not fetchable): `backend`, `title`, `url`, `doi`, `snippet`, `passage`, `reason`.

## disclosure

`tiers_used`, `services_contacted`, `identities_used`, and `requests_sent` (`service`, `tier`, `round`, templated `url`, `text_sent`, `status`, `error`, `from_cache`).

## Markdown template

```
## Coverage
Status: <plain words>. Why: <status_reasons>.
Checked: <n> of <total> cited or supplied works (full text obtained: ...). Not checked: <number, text/DOI, reason> ...
Unresolved references: ... | Corpus: ... (not loaded: ...)
Sections: checked ... / excluded ...   Open tier: <queried>/<eligible> sentences, <not queried> over the cap, <errors>
Limits: ...

## Findings
| Passage | Location | Source (DOI/URL) | License | Version | Words | Attribution | Severity |

## Informational (quoted and cited)   ## Boilerplate   ## Unverified candidates
## Disclosure: <services, identities, every request>
<disclaimer, verbatim>
```
