# Fixture provenance

Source: spike report `docs/research/2026-10-20-code-backends-spike.md` section 5. Each case is a mini-project under `cases/<ID>/`. Files are byte-exact copies (`.gitattributes` sets `* -text`). Texts were fetched through the Stack Exchange API (HTML `<pre><code>`, entity-decoded, LF) and `gh api ... -H "Accept: application/vnd.github.raw"`.

## Cases

| Id | File | Source URL / author / date / licence | How it was verified |
|---|---|---|---|
| C1 | `C1/C1_so_2019_cc4.js` (17 lines, answer's 2nd code block) | https://stackoverflow.com/a/58437909 ; Scotty Jamison ; created 2019-10-17, last edit 2024-06-19 ; CC BY-SA 4.0 | API `content_license` = CC BY-SA 4.0 ; date rule (>= 2018-05-02) |
| C1p | `C1p/C1p_so_2018_cc4.py` (13 lines, `ChainedAssignent`) | https://stackoverflow.com/a/53954986 ; coldspeed95 ; created 2018-12-28 ; CC BY-SA 4.0 | All 18 revisions `CC BY-SA 4.0` (revisions API) |
| C1b | `C1b/C1b_so_py_edited2022.py` (12 lines) | https://stackoverflow.com/a/26853961 ; Aaron Hall ; created 2014-11-10, last edit 2022-03-29 ; API now CC BY-SA 4.0 though created in the 3.0 era | API `content_license`; licence is revision-dependent |
| C2 | `C2/C2_so_2011_cc3.js` (21 lines, `syntaxHighlight`) | https://stackoverflow.com/a/7220510 ; user123444555621 ; created 2011-08-28, last edit 2017-05-31 ; CC BY-SA 3.0 | API `CC BY-SA 3.0`; revisions 8 and 9 `CC BY-SA 3.0` |
| C3 | `C3/C3_so_with_attribution.js` | CONSTRUCTED: C1 plus first-line comment "Adapted from https://stackoverflow.com/a/58437909 (CC BY-SA 4.0, Scotty Jamison)" | Constructed; derived from C1 |
| C10 | `C10/C10_renamed_of_C1.js` | CONSTRUCTED: C1 with `doAsyncTask->runFetchJob, url->endpoint, result->payload, response->res` | Constructed; derived from C1 |
| C4 | `C4/C4_attrs_filters_MIT.py` (72 lines, whole file, `# SPDX-License-Identifier: MIT` on line 1) | https://github.com/python-attrs/attrs/blob/main/src/attr/filters.py ; Hynek Schlawack ; first commit 2015-10-16, latest 2024-08-02 ; MIT | Repo API licence MIT ; sha1_git 689b1705a60ff110d6077bab996f8b4588e55b82 equals the GitHub blob SHA |
| C5 | `C5/C5_ansible_unfrackpath_GPL3.py` (34 lines, header removed), plus `C5/LICENSE` | https://github.com/ansible/ansible/blob/devel/lib/ansible/utils/path.py lines 29-62 ; header "(c) 2012-2014, Michael DeHaan" with GPL v3+ text ; GPL-3.0 | Repo API licence GPL-3.0 ; COPYING = GPLv3 ; last commit 2025-10-21 f1f5b93. `C5/LICENSE` is CONSTRUCTED (MIT text, "Example Project Authors") so C5 models a GPL function in an MIT project |
| C5b | `C5b/C5b_git_strbuf_commented_addf_GPL2.c` (18 lines) | https://github.com/git/git/blob/master/strbuf.c ; Git authors ; GPL-2.0 | COPYING = GPL v2 ; GitHub API licence field `NOASSERTION` ; file has no header |
| C6 | `C6/package.json` (declares `is-odd 3.0.1`), `C6/node_modules/is-odd/*`, `C6/node_modules/is-number/*` | npm registry tarballs `is-odd-3.0.1.tgz`, `is-number-6.0.0.tgz` ; Jon Schlinkert ; MIT | Registry licence MIT ; LICENSE files and header "Released under the MIT License" ; ClearlyDefined declared MIT. Tarballs extracted with `python -I` |
| C7 | `C7/C7_boilerplate.py`, `C7/C7_boilerplate.java` (getter/setter, imports, main) | CONSTRUCTED boilerplate | Constructed |
| C8 | `C8/C8_original.py` (13 lines) | CONSTRUCTED, original | Constructed |
| C9 | `C9/C9_nolicense_noamopt.py` (37 lines, `NoamOpt`) | https://github.com/jsbaan/transformer-from-scratch/blob/main/lr_scheduler.py ; Joris Baan ; commit 2022-03-21 ; no licence | Repo API `license: null`; root has only .github, *.py, readme.md, requirements.txt; readme has no licence; docstring says "Copied from https://nlp.seas.harvard.edu/2018/04/03/attention.html" |

## Recorded responses (`responses/`)

All recorded 2026-10-20 during the backend spike (`$SPIKE` = the spike scratchpad `code-spike/`).

| File | Endpoint | Spike file |
|---|---|---|
| `scanoss_by_md5.json` | SCANOSS free API (`/scan/direct`); keyed by MD5 of each fixture file's bytes; no match maps to `[{"id": "none", ...}]`. Only the files the spike scanned (C6 limited to `is-number/index.js` and `is-odd/index.js`) | `$SPIKE/out/scan.json` |
| `scancode_sample.json` | ScanCode Toolkit 32.5.0 local scan (`--info --license --copyright --package`); entries for C3, C4 and C6 only | `$SPIKE/out/sc.json` |
| `se_answers_spike.json` | Stack Exchange `/answers/{ids}` with `content_license` | `$SPIKE/dl/ans.json` |
| `se_answers_53954986.json` | Stack Exchange `/answers/53954986` (C1p) | `$SPIKE/dl/a53.json` |
| `se_answers_noamopt.json` | Stack Exchange `/answers` check for the `NoamOpt` posts (C9) | `$SPIKE/dl/chk.json` |
| `cd_attrs_24.1.0.json` | ClearlyDefined definition for `attrs` 24.1.0 | `$SPIKE/dl/cd.json` |
| `7f36c03800c8df50.body` (indexed in `index.json`) | ClearlyDefined `POST https://api.clearlydefined.io/definitions` with body `["npm/npmjs/-/is-odd/3.0.1"]` (C6 declared dependency); recorded live 2026-10-20 by `deps.py run --record`, HTTP 200, `licensed.declared` MIT | none (live capture; public package coordinate only) |
| `index.json` | Replay index (`request_key` -> recorded file, status, headers); entries are added by `RecordingOpener` | none |

## Licence of these fixtures

The files keep their original licences: CC BY-SA 3.0 / 4.0 for the Stack Overflow snippets (C1, C1p, C1b, C2 and the derived C3, C10), GPL-2.0 / GPL-3.0 for C5b and C5, MIT for C4 and C6. They live only under `tests/fixtures/cases/`, are used for testing only, and await the repository owner's licence decision.
