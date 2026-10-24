# Privacy: what leaves the machine

Nothing derived from your work leaves the machine unless you approved the exact request. Every networked script has two phases: `plan` writes a query file listing every outgoing request verbatim with its sha256, and `run --sha256 <hash>` sends only what that plan lists (and nothing at all if the hash does not match). `report.py` makes no network calls.

The guarantee covers the scripts' own requests. Code that Claude reads while helping you is already in the model's context; the plan/run gate does not change that.

## Tiers

| Tier | Script | Service | What is sent | Default |
|---|---|---|---|---|
| local | `inventory.py`, `report.py` | none | nothing | always |
| strict | `deps.py` | api.clearlydefined.io | public package coordinates (ecosystem, name, exact version) of declared dependencies | offered; needs your yes |
| fingerprint | `scanoss_backend.py` | api.osskb.org (SCANOSS) | a sanitised fingerprint file for the files you chose with `--include` | opt-in; needs `--include` and your yes |
| open | `stackexchange.py` | api.stackexchange.com | 1-3 identifiers or words per code block from the files you chose, then public post ids returned by the service | opt-in; needs `--include` and your yes |

You choose the files for the fingerprint and open tiers (`--include` has no default). Sensitive files (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `credentials*`, `secrets*`), vendored directories, generated, minified, binary, oversized and non-UTF-8 files are never eligible.

## Strict tier (ClearlyDefined)

One `POST https://api.clearlydefined.io/definitions` per batch of up to 50 coordinates such as `npm/npmjs/-/is-odd/3.0.1`. Sent: the package name and exact version of direct dependencies declared in `package.json`, `package-lock.json`, `requirements*.txt` and `pyproject.toml` (npm and PyPI only). Not sent: dependencies with ranges or local/git specs, dependencies the manifests show come from a private registry or index, file paths, file contents, the project name. Scoped npm packages go in their own requests so a whole scope can be struck. Names are public information once sent: strike any request that holds a name that looks internal.

Known limit: a private registry configured outside the files read (for example `[tool.hatch.index]`, `PIP_INDEX_URL`/`UV_INDEX_URL` in `.env`, CI files or Dockerfiles, or configuration above the scan target) is not detected. Registry variables of the running process (`NPM_CONFIG_REGISTRY`, `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, ...), `~/.npmrc` and user-level `pip.conf` are read and, when they name a non-public registry or cannot be read, the names are withheld. Anything not detected, so the names of private dependencies there may be sent. Always review the preview and strike requests. On Python older than 3.11 (no TOML reader) the dependencies in directories that hold `pyproject.toml`, `uv.toml`, `poetry.toml` or `Pipfile` are excluded rather than guessed.

## Fingerprint tier (SCANOSS)

Disclosure shown in every preview (N is the number of files in the plan):

- This uploads one fingerprint file (WFP) to the Software Transparency Foundation's free SCANOSS service at api.osskb.org, which may log usage to monitor service load; no retention period is published.
- For each selected file the WFP contains: an MD5 hash of the whole file, an MD5 hash of the file with its line endings flipped, and about one 32-bit hash per few lines together with the real line numbers. It contains no source text, but anyone holding these hashes can check whether a known file or passage is present.
- File names, paths and sizes are not sent: files are renamed 1.ext, 2.ext, … (the extension is kept) and sizes are set to 0. The mapping back to your paths stays on this machine.
- The request also carries your IP address, the time, and the client name and version (scanoss-py).
- Free-tier limits: 5000 files/day, 7500/week, 15000/month. This plan uses N files. The service's terms allow software identification only. The service describes itself as "designed for professional use only and not offered for end-consumer use" and limits OSSKB output to software identification (Software Transparency Foundation terms, https://www.softwaretransparency.org/terms).
- The client uploads the fingerprint file in several smaller POST requests (about one per 32 files or 32 KB), each with its own random request id, plus the headers user-agent scanoss-py/<version>, x-scanoss-client (tool, version, build date), and a form field format=plain; N requests are expected for this plan.

The `scanoss-py` client runs with `SCANOSS_API_KEY` and `SCANOSS_SCAN_URL` removed from its environment, with no key and `--apiurl https://api.osskb.org`. Only the pinned client range (1.54.x) was reviewed for what it uploads; other versions are refused unless you pass `--allow-untested-client`. The terms: `references/scanoss-terms.md`. Free-tier limits: 5000 files/day, 7500/week, 15000/month; the default cap is 200 files per plan (`--max-files`, at most 500).

## Open tier (Stack Exchange)

Disclosure shown in every preview:

- Sent: for each code block you chose, 1 to 3 identifiers or words taken from your code (the rarest ones) as the q= parameter of a search request to api.stackexchange.com, a public service. Anyone who can see these requests can read those words: strike any request whose words look internal or private.
- Filtered out as query words (best effort, not a guarantee): words that look like a secret, a key, an e-mail address, an IP address, a host name that looks internal, or a path. A name that is private to you but looks ordinary is NOT recognised, so check the words in each request. Not sent at all: file contents, file names or paths, comments, the project name.
- Follow-up requests carry only public post ids that the search returned (answers, then revisions of the answers that matched). They carry nothing from your code.
- Stack Exchange also sees your IP address, the time of each request and the client name. Without --use-key the requests are anonymous (no API key); the anonymous quota is 300 requests per day per IP.
- Matching happens on this machine: the returned answer code is compared with your code locally.

Per code block the plan holds up to four searches: the rarest word (pages 1 and 2), the two rarest words together, and the second rarest word alone. Words are the rarest identifiers, hostnames or single words from string literals; names on a common-words list, and anything that looks like a secret, key, e-mail address, IP address, internal host name or path, are never used. The default cap is 30 search requests per plan (`--max-requests`), about seven code blocks. Follow-up requests (`/answers/{ids}`, `/posts/{ids}/revisions`) carry only post ids the search returned.

Quota: 300 requests/day anonymous per IP, 10,000/day with a key. No API key is sent unless you pass `--use-key`; the key is read from the `STACKEXCHANGE_KEY` environment variable at send time and the plan holds only the marker `{secret:STACKEXCHANGE_KEY}`. The `backoff` field is honoured (bounded), and HTTP 429, `quota_remaining: 0` and error 502 stop the run and list everything not sent.

## Plans, results and what stays local

Plan files hold the requests plus a `local_only` section (the neutral-name to path map, block hashes) that is never sent. Results files hold candidate origins and your relative file paths. Keep both outside the project and treat them as local data. Recorded responses used by the tests contain only public fixture identifiers.
