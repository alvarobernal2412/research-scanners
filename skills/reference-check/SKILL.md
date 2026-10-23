---
name: reference-check
description: Verifies that every reference in a paper exists and matches the real record (authors, title, year, venue, DOI) by checking Crossref, DataCite and OpenAlex. Use when asked to check, validate or verify a bibliography or reference list, find hallucinated or fabricated citations, or confirm DOIs resolve to the cited work. Reports evidence only; gives no verdict on who or what wrote the paper.
license: Apache-2.0
---

# reference-check

Checks each reference of a paper against public bibliographic records. A bibliography with references that do not exist, or whose DOI points to a different work, is the usual sign of unchecked machine-generated citations; this skill reports those facts and nothing about authorship.

## When to use

- The user asks to verify, validate or audit references, citations, a `.bib` file or a reference list, or to find hallucinated/fabricated citations.
- Input: `.bib`, `.tex` (with `\bibitem` or a `\bibliography` file next to it), `.bbl`, `.md`, `.txt`, `.docx`, `.pdf`.

Do not use for: checking whether a cited work supports a claim, plagiarism (use text-plagiarism), or deciding whether an AI wrote the paper.

## Run

Install once if you need `.docx` or `.pdf` input: `pip install -r <skill dir>/requirements.txt`. `.bib`, `.tex`, `.md` and `.txt` need nothing.

```
python -I <skill dir>/scripts/check_refs.py <file> [--offline] [--limit N]
```

`<skill dir>` is the directory this SKILL.md was loaded from. Optionally set `CROSSREF_MAILTO` to your email so Crossref and OpenAlex serve you from their polite pools (it appears in the User-Agent).

**What is sent:** for each reference its DOI or arXiv id, or its title, first authors and year, to api.crossref.org, api.datacite.org, api.openalex.org and doi.org. Never the paper's body text. Tell the user once that the reference list reveals the paper's topic. `--offline` extracts and parses only.

## Read the JSON

- `error`: `file_not_found`, `unsupported_format`, `unreadable_file`, `no_text_extracted` (scanned PDF), `no_references_found`, `missing_dependency` (see `detail`). Tell the user and stop.
- `references[]`: `verdict`, `differences` (field, cited value, record value), `record` (what the index holds), `via` (`doi`, `arxiv` or `title_search`), `note`, and `alternative_record` when an unregistered DOI's title exists under another DOI.
- Verdicts: `verified`; `verified_with_differences` (found, minor differences such as year off by one or a different venue name, listed); `mismatch` (the DOI or title leads to a record with a different title, first author or year); `doi_not_registered`; `not_found` (no accepted match in the indexes queried); `unverifiable` (web page, too little information, or a DOI outside Crossref/DataCite); `lookup_failed` (a service did not answer); `unchecked` (offline).
- `needs_attention`: the mismatch, doi_not_registered, not_found and lookup_failed entries, most serious first.
- `coverage`, `services_contacted`, `requests_log`, `service_errors`.

## Write the report

1. Counts per verdict, then a table of `needs_attention` entries with the cited text, what was found, and the differences. Show the reference as the user wrote it next to the record.
2. Say what could not be checked (`unverifiable`, `lookup_failed`, heuristically parsed entries) and list the services contacted.
3. For `mismatch` and `doi_not_registered` suggest the user compare with the publisher page; if `alternative_record` exists mention it as a possible correct DOI.
4. Wording rules: `not_found` is "not found in Crossref, DataCite and OpenAlex", never "fabricated" or "fake". Never say or imply the paper was AI-generated. Never describe a reference list as "clean" or "all valid" unless every entry is verified, and still list the coverage limits.

## Limits to state

- Books, theses, standards, websites and grey literature are often missing from these indexes.
- Free-text references are parsed heuristically (BibTeX is reliable). A wrong parse can cause a false `not_found`; check those by hand.
- Index metadata can itself contain errors. The skill checks existence and match, not whether the cited work supports the claim.
