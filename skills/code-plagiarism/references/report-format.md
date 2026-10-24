# Report format

Input: the JSON printed by `report.py`. Output: a Markdown report. Start with the `summary`.

## Findings table

One row per finding, flagged findings first.

| File:lines | Match | Candidate origins (url, author, date) | Licence (evidence, confidence) | Attribution | Obligation |
|---|---|---|---|---|---|
| `src/a.js:4-17` | `exact` (100%) | https://stackoverflow.com/a/58437909, Scotty Jamison, 2019-10-17 | CC-BY-SA-4.0 (`so_revision`, high) | absent | If copyrightable, reuse is governed by CC-BY-SA-4.0: credit the author ... |

- File and lines come from `file` and `lines`. For flagged findings show `matched_lines` in a code block.
- Match is `match_type`: `exact` (same tokens), `normalised` (same after renaming identifiers), `near` (similar with differences, `matched_pct` shows how much), `file` (whole file).
- List every candidate of a finding (`candidates[]`) with its URL, author, date, licences with `evidence` and `confidence`, and which backends found it (`backends`). Several candidates in one finding are alternatives, not a chain.
- Attribution is `attribution.status`: `present` (a reference to the candidate or a header licence equal to the candidate's), `other_reference` (the file cites something else), `absent`. Quote `attribution.evidence`.
- Flags: `unattributed`, `license_conflict`, `no_license_all_rights_reserved`. Explain them in one line each and add the `notes`, including the note that candidates are not origins.
- Obligation is `obligation`, verbatim.

## Beyond the table

- `dependencies`: a short table of name, version, licence, evidence. Note that dependency source was not compared.
- `references_in_comments`: mention URLs the code itself cites.
- `dropped`: say how many matches were ignored and why (below threshold, boilerplate, declared dependency).
- Coverage: `tiers_run`, `services_contacted`, the `requests` actually made (method, URL, status), `errors` and `not_sent` (each means part of the scope was NOT checked), `skipped_files` by reason, `checked` and `not_checked`, `scancode`, `kb_version`, and `notes`.
- With zero findings write the `summary` sentence ("No matches found in the checked scope") and the `not_checked` list. Do not write that the code is original or clean.
- End with `disclaimer`, verbatim: "Evidence of textual similarity and licence information only; not a legal opinion and not a finding about who copied from whom."
