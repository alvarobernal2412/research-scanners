---
name: code-plagiarism
description: Checks a codebase for code taken from Stack Overflow answers, open-source projects and other licensed sources, and reports candidate origins, licence evidence (per file header, per Stack Overflow revision), attribution status and obligation text. Use when asked about copied or reused code, licence compliance of snippets, missing attribution, or "did this come from Stack Overflow / GitHub". Report only: no legal verdict and no claim about who copied from whom. Every step that sends data derived from the code needs the user's approval of the exact request.
license: Apache-2.0
---

# code-plagiarism

Finds code in a project that matches public sources (Stack Overflow answers, open-source repositories) and reports, for each finding, the candidate origins, the licence evidence and where it comes from, whether attribution already exists in the file, and what the licence asks of the user. It is a lead generator for the user's own review.

## When to use

- The user asks whether code was copied, reused or taken from Stack Overflow, GitHub or elsewhere, whether snippets are properly attributed, or what licence applies to a snippet.
- Input: a project directory (JavaScript/TypeScript, Java, C/C++/C#, Go, Rust, Kotlin, Swift, PHP, Scala, Dart, Python, Ruby, shell, R, Perl).

Do not use for: comparing student submissions with each other, legal advice, or deciding who wrote code.

## Install

Python 3.10+, standard library only. Nothing needs installing for the strict and open tiers. `<skill dir>/requirements.txt` lists only the optional SCANOSS client. Optional:

- `pip install "scanoss>=1.54,<1.55"` (MIT) for the fingerprint tier. The scripts look for the `scanoss-py` command.
- ScanCode Toolkit (`pip install scancode-toolkit "pluggy==1.5.0"`, about 670 MB) for stronger local licence detection. Without it a built-in detector reads SPDX tags and common licence texts.

`<skill dir>` is the directory this SKILL.md was loaded from. Run every script as `python -I <skill dir>/scripts/<name>.py`; each prints JSON, and on failure `{"error": "<code>", "detail": ...}` with exit code 2. Tell the user the error and stop.

Write plan and results files OUTSIDE the project being scanned (for example a temp directory); `plan --out` refuses to overwrite an existing file, so use a fresh name each time.

## Tiers

| Tier | Sends | Needs |
|---|---|---|
| local (always) | nothing | nothing |
| strict (`deps.py`) | public package names and exact versions to api.clearlydefined.io | the user's yes to the preview |
| fingerprint (`scanoss_backend.py`) | a sanitised fingerprint file (hashes, neutral file names, sizes 0) to api.osskb.org | `--include`, the user's yes to the preview |
| open (`stackexchange.py`) | 1-3 identifiers per code block to api.stackexchange.com | `--include`, the user's yes to the preview |

Details of what leaves the machine: `references/privacy.md`.

## Workflow

1. Inventory. `python -I <skill dir>/scripts/inventory.py <root>`. Show the user the eligible files grouped by directory (`eligible_by_dir`) and the skipped counts with reasons (`skipped`: vendored, generated, minified, sensitive, binary, too_large, non_utf8, gitignored, not_source). `no_source_files` means there is nothing to scan.
2. Local report. `python -I <skill dir>/scripts/report.py <root>` (offline, `strict-local`). It lists licence headers, SPDX tags, references to Stack Overflow/GitHub in comments, and the coverage. With no backend results it can only say "No matches found in the checked scope" and which tiers were not run.
3. Offer the strict tier. `python -I <skill dir>/scripts/deps.py plan <root> --out <plan.json>` prints `{plan_path, sha256, preview}`. Show `preview` verbatim. Only after the user says yes to that plan: `python -I <skill dir>/scripts/deps.py run <plan.json> --sha256 <sha256> --out <results.json>`.
4. Offer the fingerprint and open tiers. Ask which files or directories are eligible and list the candidates from step 1; `--include` is required and has no default. Never include files the user did not choose.
   - `python -I <skill dir>/scripts/scanoss_backend.py plan <root> --include <path> [<path> ...] --out <plan.json> [--max-files N]`
   - `python -I <skill dir>/scripts/stackexchange.py plan <root> --include <path> [<path> ...] --out <plan.json> [--use-key] [--max-requests N]`
   Show `preview` verbatim (for SCANOSS this includes the full fingerprint file and the disclosure; for Stack Exchange every query word). The user may drop requests: `python -I <skill dir>/scripts/approval.py strike <plan.json> --ids <id1>,<id2> --out <new-plan.json>` prints the new `sha256`; show the new preview again. `python -I <skill dir>/scripts/approval.py preview <plan.json>` re-shows any plan.
   Run only after an explicit yes to that exact sha256:
   - `python -I <skill dir>/scripts/scanoss_backend.py run <plan.json> --sha256 <sha256> --out <results.json>`
   - `python -I <skill dir>/scripts/stackexchange.py run <plan.json> --sha256 <sha256> --root <root> --out <results.json>`
5. Report. `python -I <skill dir>/scripts/report.py <root> --results <results1.json> [<results2.json> ...] [--no-scancode]`. Results files must come from the same `<root>` (`results_root_mismatch` otherwise).

`--use-key` (Stack Exchange) reads the key from the `STACKEXCHANGE_KEY` environment variable at send time; it is never written to a file. Do not pass `--use-key` unless the user asks.

## Approval honesty

The scripts cannot tell the user from Claude. Approval is the user's yes, relayed by Claude, plus Claude Code's permission prompt for the `run` command. Never run a `run` command without an explicit yes to that exact `--sha256`; a plan the user has not seen is not approved, and a changed plan has a new hash. If the user declines or does not answer, send nothing and say which tier was not run.

The guarantee covers only what the scripts themselves send: `run` refuses any request not in the approved plan. Code that Claude reads while helping is already in the model's context, so these checks do not make the code more private than reading it did.

## Read the report

- `findings[]`: `file`, `lines`, `matched_lines`, `match_type` (`exact`, `normalised`, `near`, `file`), `candidates[]` (origin URL, author, date, revision, `licenses` with `evidence` and `confidence`, `matched_pct`, `backends`, `obligation`), `local_license_evidence`, `attribution` (`present`, `other_reference`, `absent`, with evidence lines), `flag` and `flag_reasons` (`unattributed`, `license_conflict`, `no_license_all_rights_reserved`), `obligation`, `notes`.
- `references_in_comments`, `dependencies` (declared dependencies and their licences), `dropped` (matches ignored, with the reason), `project_license`.
- `coverage`: `tiers_run`, `services_contacted`, `requests` (every request actually made), `not_sent`, `errors`, `skipped_files`, `checked`, `not_checked`, `scancode`, `kb_version`, `notes`.
- `summary` and `disclaimer`.

## Present the report

1. Start with `summary`. Then a table of findings: `File:lines | Match | Candidate origins (url, author, date) | Licence (evidence, confidence) | Attribution | Obligation` (see `references/report-format.md`). Flagged findings first. Show matched lines for flagged findings.
2. Give the coverage: tiers run, services contacted and the requests made, what was not checked, every `errors` and `not_sent` entry. If any tier failed or was skipped, say so in the first paragraph.
3. End with the `disclaimer` verbatim.

Wording rules (hard):

- Candidates are leads. Say "candidate origin", never "the source". Similarity cannot establish who copied from whom or which copy is the original; do not assert a direction of copying or one true origin. A repository match may itself hold a copy of a Stack Overflow answer.
- Never say the code is "clean". Zero findings means "no matches found in the checked scope", followed by the coverage limits (`not_checked`).
- No legal verdict: do not say "infringement", "violation", "illegal" or "stolen". Obligations are conditional: "if copyrightable, ...". Say a licence conflict is a reason to look, not a conclusion.
- No licence found means all rights reserved by default, never public domain. A per-file header or SPDX tag beats a repo-level LICENSE. A Stack Overflow licence is per revision; the date rule is only a fallback and the report says which source it used (`references/licensing.md`).
- Never state or imply who wrote the code, or whether an AI wrote it.

## Limits to state

- Public sources only; private or unpublished code, and renamed or restructured copies beyond token normalisation, are not found.
- Short or boilerplate snippets are ignored by design. Stack Exchange search is keyword based and can miss a copy that is there.
- The SCANOSS Stack Overflow data is a snapshot (about 2022); its first match for a snippet can be a repository copy rather than the original.
- The strict tier lists declared direct dependencies and their licences (npm and PyPI manifests only); it does not download dependency source.
- Quotas: Stack Exchange 300 requests/day anonymous; SCANOSS 5000 files/day anonymous.
