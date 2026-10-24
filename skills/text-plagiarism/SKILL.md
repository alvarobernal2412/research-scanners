---
name: text-plagiarism
description: Checks a paper, thesis, report or draft for passages copied word for word from published open-access papers (book hits are only snippet-level unverified candidates), and for copied text that lacks quotation marks or a citation. Reports the source (DOI or URL), its license, the match length and how the passage is attributed (quoted and cited, cited only, unattributed), always opening with a coverage section that says what could not be checked. Use when asked to check for plagiarism, copied or unattributed passages, or missing quotation of cited sources. Privacy-gated and report-only: no legal verdict, no score, no claim about who wrote the text.
license: Apache-2.0
---

# text-plagiarism

Finds verbatim and near-verbatim overlap between a document and published open-access text, classifies how each overlap is attributed, and reports it together with an honest account of what could not be checked. It reports evidence. It never decides whether something is plagiarism.

## When to use

- The user asks to check a paper, thesis, report or draft for plagiarism, copied or unattributed passages, or for quotations that are missing their quotation marks or citation.
- Input: `.pdf` (with a text layer), `.docx`, `.md`, `.txt`, `.html`, `.tex`.

Do not use for: deciding whether an AI wrote something (use llm-tropes), checking that references exist (use reference-check), or paraphrase and translation detection (not supported).

## Setup

`pip install -r <skill dir>/requirements.txt` (pypdf and python-docx; Python 3.10+). `<skill dir>` is the directory this SKILL.md was loaded from. Run every script as `python -I <skill dir>/scripts/<name>.py ...`; each prints JSON, and an error is `{"error": code}` with exit code 2.

## What may leave the machine

- **Strict tier (default).** Only public bibliographic data: DOIs, arXiv ids, URLs and reference-list strings (published data), sent to Crossref, OpenAlex, Zenodo and the hosts of open-access copies. Never the document's own sentences. It downloads the open full text of the works the document cites and compares locally.
- **Open tier (opt-in per run).** After the user picks sections and approves the exact phrases, it sends those phrases to Europe PMC and OpenAlex (and, only if requested, CORE, Open Library or Brave). Every hit is then verified locally by fetching the open text of the hit from a public URL.
- **Local corpus (`--corpus`).** Files the user supplies are compared on this machine and never produce a request.

Every networked step is two-phase: `plan` writes the exact requests to a file and prints its sha256, `run` sends only requests in a plan with that sha256. The scripts cannot tell the user from Claude. Approval is relayed by Claude, and Claude Code's permission prompt on each `run` command is the real gate, so show the preview, wait for an explicit yes on exactly that list, and only then run. The guarantee covers only requests made by the skill's scripts; text that Claude reads is already in the model's context. Details: `references/privacy-and-approval.md`.

## Flow

1. **Prepare** (no network): `python -I <skill dir>/scripts/prepare.py <document> [--workdir W] [--corpus PATH ...]`. Show the user the sections (`default_open` marks the usual candidates for the open tier, `excluded` marks references, acknowledgements and statements, which are never compared), the reference count and `warnings`. Stop on an `error` (`file_not_found`, `unsupported_format`, `unreadable_file`, `no_text_extracted` for empty or scanned documents, `missing_dependency`). `glued_words` means PDF extraction lost the spaces: say that the result will be `insufficient_coverage`.
2. **Strict tier.** Explain what is sent (see above) and ask once. Then loop: `python -I <skill dir>/scripts/strict.py plan --workdir W` prints the next round (`resolve`, `locate`, `zenodo`, `download`), its `plan` file, `sha256` and `preview` (including `local_only` corpus files, which are not in the plan). Show the preview, then `python -I <skill dir>/scripts/strict.py run --workdir W --plan P --sha256 H`. Repeat until `plan` prints `{"done": true}`.
3. **Open tier**, only on request or when coverage is insufficient. List the sections with their defaults and let the user choose. Run `python -I <skill dir>/scripts/open_tier.py plan --workdir W --sections s1,s2 [--backends europepmc,openalex] [--include c4,c9] [--use-keys]`. Show every exact phrase grouped by service with the identities used, the sentences excluded (digits, equations, novel terms, quoted text) and the count not queried because of the cap (10 sentences per run; the optional second OpenAlex window is planned and approved separately). The user may strike requests (`python -I <skill dir>/scripts/approval.py strike <plan> --ids r3,r7`, which gives a new sha256 to approve; `approval.py show <plan>` re-displays a plan) or re-add excluded sentences by id (`--include`, then plan again). Run only after an explicit yes on that list: `python -I <skill dir>/scripts/open_tier.py run --workdir W --plan P --sha256 H`. Then call `plan` without arguments again: it produces an optional second OpenAlex window and the `verify` round, which sends only public URLs of hit texts. Run each the same way until `{"done": true}`. Brave needs `BRAVE_API_KEY` (plan fails with `missing_key` otherwise).
4. **Report**: `python -I <skill dir>/scripts/report.py --workdir W` (no network). Present it as described below.

Offline use: run only `prepare.py` and `report.py` (optionally with local `--corpus` files). No request is made. The status is `insufficient_coverage` unless at least three local corpus files were compared and every other cited work is known to have no open copy; `status_reasons` says what was not checked.

## Optional reference corpus

`--corpus PATH` (repeatable) adds works to compare against. Accepted: a document file, a directory (recursive), a `.bib` file, a `.txt` list of DOIs, arXiv ids or URLs (one per line, `#` comments), and a `corpus.json` manifest (`[{"file", "doi", "title", "authors", "year", "relation": "related"|"own", "license"}]`) in a directory or passed explicitly. Local files never leave the machine; identifier entries are public identifiers and join the strict rounds. `relation: "own"` reports overlap with the user's own earlier work as `self_overlap` (never above severity `light`) and must be worded that way. This is how paywalled or unpublished sources the user already holds become checkable: advise supplying the PDF. `python -I <skill dir>/scripts/corpus.py status --workdir W` lists the items. Items that cannot be loaded are listed in `coverage.corpus.not_loaded`; an identifier whose text cannot be fetched is `full_text_not_obtained` with reason `paywalled_provide_pdf`.

## Present the report

Coverage first, always, as Markdown:

1. **Coverage**: `coverage.status` in plain words, `status_reasons`, tiers used, extraction warnings, references checked and not checked (each with its reason, by number and text), unresolved references, corpus items and any not loaded, sections checked and excluded, open-tier counts (eligible, queried, excluded by filter, not queried over the cap), backend errors, and `limits`.
2. **Findings** table: passage, location (section, lines, page), source (DOI or URL), license, version, overlap words, attribution, severity, confidence. Add a note for `cites_other_source` and for `kind: self_overlap`.
3. **Informational** (quoted and cited), **boilerplate** (common phrasing, many sources), **unverified candidates** (snippet-only or unfetchable hits: never findings).
4. **Disclosure**: every service contacted, identity used and request sent.
5. The `disclaimer` verbatim.

Statuses: `findings`, `no_overlap_found_in_checked_sources`, `insufficient_coverage`. Never present a no-finding result as "clean" or as proof of originality: it means only that nothing matched in the sources that could be checked. Never give a legal verdict, a percentage, a score or any statement about who wrote the text. Never re-share downloaded full text; quote only the matched passages the report carries.

## Optional keys

All off unless set: `OPENALEX_API_KEY` (query parameter), `CORE_API_KEY` (bearer header), `BRAVE_API_KEY` (header; Brave is keyed-only), `TEXT_PLAGIARISM_MAILTO` (Crossref polite-pool User-Agent), `TEXT_PLAGIARISM_CACHE` (cache location). Open-tier requests carry no key or address on a keyless route unless `--use-keys` is passed. Plans store `{ENV_NAME}` placeholders, never values. Unpaywall is not used.

## Known limits to state

Paywalled sources cannot be checked unless an open copy exists or the user supplies the PDF; unpublished, private and unindexed sources cannot be detected; paraphrase and translation are not detected; OpenAlex full-text search is stemmed, covers a fraction of works and missed about 40% of true sentences in testing; Europe PMC covers open-access biomedical text; CORE body text is unverified. Thresholds (`SHINGLE_K`, `MIN_MATCH_WORDS`, ...) are provisional until calibrated on the user's own cases.

## Evaluation

`python -I <skill dir>/scripts/evaluate.py <cases.json> [--responses DIR] [--live]` scores the skill on labelled cases (precision, recall, known limitations, and the privacy, coverage and no-verdict gates). Offline by default: rounds are answered from recorded responses and nothing leaves the machine; `--live` uses the network only for cases marked `public_text`. Maintainer tool, not part of a normal check.

More: `references/backends.md` (what each service searches and what was measured), `references/report-format.md` (JSON fields and the Markdown template).
