# Licence evidence and obligations

The report is evidence, not a legal opinion. Obligations are written conditionally ("if copyrightable"): short or trivial snippets may not be protected at all, and whether a licence applies depends on facts the scripts cannot see.

## Precedence of licence evidence

1. A per-file header or SPDX tag (`file_header`, high confidence) always beats a repo-level LICENSE or the package manifest's `license` field (`repo_license`, `package_manifest`). Bare mentions of a licence name in a file (medium) never displace a project licence; they are used only when the project has none.
2. ScanCode results (`scancode`) are merged as extra file evidence when ScanCode is installed.
3. No licence found anywhere means all rights are reserved by default. The report says so; it never treats missing licence information as public domain.

The report's `local_license_evidence` lists what was found in the file (with line, source and confidence), `project_license` lists what was found for the project, and each finding's `notes` names the effective licence and where it came from.

## Stack Overflow

- The licence is per revision of the answer, not per answer. The open tier fetches the revisions of an answer that matched and reports the licence of the earliest revision whose code matches the user's code (`so_revision`, high confidence). Example: answer 26853961 was created in 2014 but the matching snippet was added in a 2022 edit, so the matching revision is CC BY-SA 4.0.
- If revision bodies are not available the answer's current `content_license` is reported (`so_answer_current`, medium), and, when it differs, the date rule (`so_date_rule`, low) is listed too. The date rule: content before 2011-04-08 (UTC) is CC BY-SA 2.5; from 2011-04-08 to before 2018-05-02 CC BY-SA 3.0; from 2018-05-02 CC BY-SA 4.0. It is only a fallback because a later edit changes the licence of that revision.
- SCANOSS knows a snapshot revision of a post (`scanoss_component_declared`, medium); the live post may differ.
- Obligation text (all versions require attribution; ShareAlike applies to adaptations): credit the author, link the answer, name the licence with a link, indicate changes, share adaptations under the same or a compatible licence.

## Copyleft and permissive candidates

- Copyleft families named by the report: GPL, AGPL, LGPL, MPL and CC BY-SA. A copyleft candidate licence that differs from the file's effective licence (the file header, else the project licence) is flagged `license_conflict`. That flag is a reason to look. Whether it matters depends on whether the code was copied, how, and how it is distributed.
- For a permissive candidate (MIT, BSD, Apache and similar) the obligation is to keep the copyright and licence notice with the copied code.
- A file whose header names the same licence as the candidate counts as attribution present.

## Why candidates are not origins

- A match shows where similar code exists. It cannot show who copied from whom, which copy is the original, or that the user's code was copied at all.
- SCANOSS often returns a repository that holds a copy of a Stack Overflow answer. Example (C2, an answer from 2011): SCANOSS matched an MIT-licensed GitHub repository, while the Stack Exchange search finds the answer itself under CC BY-SA 3.0. MIT is never stated to apply to the user's file; the report lists both as candidates and says the origin is not established.
- A match against a repository with no licence file (C9) is reported as "no licence found: all rights are reserved by default". The matched repository may in turn say it was copied from a public web page.
- The SCANOSS Stack Overflow data is a snapshot (about 2022). Newer answers are not in it; the open tier covers them.

## Dependencies

Declared direct dependencies with an exact version are listed with the licence ClearlyDefined declares (`clearlydefined`). Their source is not downloaded and not compared. Files inside vendored directories (for example `node_modules`) are never findings; they are counted under `skipped_files.vendored`.
