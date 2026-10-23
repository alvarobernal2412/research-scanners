---
name: license-compliance
description: Checks whether the licenses of a software project's dependencies (Python, npm) are compatible with the project's own license, lists dependencies that cannot be used as-is, and recommends a license (naming the ones ruled out) when the project has none. Use when asked about license compatibility, dependency licenses, "can I release this under MIT", or which license to choose. Reports evidence only; not legal advice.
license: Apache-2.0
---

# license-compliance

Compares the licenses of a project's dependencies with the project's own license. Report-only: it never edits the license file or the manifests, and it gives no legal conclusion.

## When to use

- The user asks whether their dependencies' licenses are compatible with their project's license, what license to pick, or which dependencies they cannot ship.
- The project is a web app, Python library, CLI or similar with `requirements*.txt`, `pyproject.toml`, `Pipfile.lock`, `poetry.lock`, `package.json` or `package-lock.json`.

Do not use for: finding copied code (use code-plagiarism), or giving a legal opinion.

## Run

```
python -I <skill dir>/scripts/check_licenses.py <project dir> [--offline] [--include-dev] [--project-license SPDX]
```

No Python packages need installing (Python 3.10+, standard library only). `<skill dir>` is the directory this SKILL.md was loaded from.

- Default: license data comes from the lockfile, from locally installed metadata (`.venv`, `node_modules`), and from PyPI / npm. **Only registry package names and versions are sent.** Dependencies installed from git, a path or a URL are never sent (their names may be private).
- `--offline`: no network at all; only lockfile and local metadata.
- `--include-dev`: also look up development-only dependencies (still not judged).
- `--project-license`: use this SPDX expression instead of the detected one (for "what if I used X").

## Read the JSON

- `error`: `not_a_directory` or `no_manifests_found`. Tell the user and stop.
- `project.license` and `project.evidence`: the detected license and where it came from. `null` means none was recognised.
- `judged_against`: the license dependencies were judged against. `basis` is `project`, or `recommended` when the project has no license.
- `dependencies[]`: each has `license`, `license_evidence`, `license_confidence`, `lookup_status` and a `verdict`: `compatible` (with `conditions` such as "keep the notice"), `conflict` (with `why`), `needs_review` (with `reason`: `no_license`, `unknown_license`, `non_free_license`, `exception_clause`, `dual_license_ambiguous`, `unregistered_source`, `lookup_failed`) or `not_judged` (development-only).
- `conflicts`, `not_usable_as_is`, `needs_review`: the same items grouped.
- `recommendation` (present when the project has no license, or its license conflicts): `recommended`, `usable` licenses, `not_usable` licenses each with `blocked_by` dependencies, `unresolved_dependencies`, `caveats`.
- `coverage`: what was not checked. `requests_log`: every request sent.

## Write the report

1. Start with the project's license and its evidence (or say none was found).
2. Table of dependencies that conflict or need review, with the license, the reason, and the evidence source. Mention conditions (notice files, source offers) for compatible copyleft and Apache dependencies briefly.
3. If a recommendation exists, present the recommended license and why, then the licenses that cannot be used and which dependency blocks each. Explain that "no license" on the project means all rights reserved, so users cannot legally reuse it.
4. Always include the coverage section and the list of services contacted (from `requests_log`).
5. Never write "safe", "legal", "clean" or "violation". Zero conflicts means "no conflicts found among the checked dependencies".

## Limits to state

- Transitive dependencies are only known with a lockfile or fully pinned requirements.
- Registry license data is self-declared and can be wrong, missing or ambiguous. A dependency with no license is not usable as-is (all rights reserved by default).
- Compatibility depends on how code is combined and distributed (linking, bundling, network use). Rules are an approximation of published guidance (`data/compat.json`), not legal advice.
- Vendored or bundled code inside dependencies is not inspected. Only Python and npm are supported.
