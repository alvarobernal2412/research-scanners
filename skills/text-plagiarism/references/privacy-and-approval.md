# Privacy and approval

## Tiers

| Tier | May carry | Never carries | Opt-in |
|---|---|---|---|
| strict (default) | `public_id` (DOIs, arXiv ids), `reference_string` (a reference-list entry, which is published bibliographic data), `public_url` | any sentence of the document | no |
| open | all of the above plus `user_text`: exact phrases from sections the user chose, shown in a preview and approved | sentences from excluded sections, sentences with digits, equations or novel terms unless re-added by id, more than 10 sentences or 10 requests per backend per run | yes, per run |
| local corpus | nothing: files are read and compared on this machine | their paths and text appear in no plan or request | `--corpus` |

There is no fingerprint tier for text. `build_plan` raises for a strict plan containing `user_text`, and `load_approved_plan` rejects a strict-tier plan file that was edited to contain it.

## Plan file

`plans/strict-NN-<round>.json` and `plans/open-NN-<round>.json` inside the run directory, written as canonical JSON (sorted keys, UTF-8):

```
{"version": 1, "tier": "strict|open", "round": "resolve|locate|zenodo|download|search|search2|verify",
 "document_sha256": "...", "requests": [
   {"id": "r001", "service": "crossref", "method": "GET", "url": "...", "carries": "reference_string",
    "purpose": "...", "subject": "ref:0", "headers": {}, "body": null, "text_sent": null, "identity": null}]}
```

`sha256` is the hash of the file's bytes. `run` refuses a plan whose hash differs from the one given (`plan_hash_mismatch`), that does not exist (`plan_missing`), that is malformed (`plan_invalid`) or that was made for another document (`plan_document_mismatch`). `net.send` additionally checks each request against the approved plan (method, URL, headers, body, `carries`, `text_sent`) before opening a connection and raises for anything else.

`subject` links a response to what it concerns (`ref:<index>`, `source:<key>`, `zenodo:<key>:<id>`, `cand:<id>`, `hit:<url-hash>`). Secrets appear only as `{OPENALEX_API_KEY}`, `{CORE_API_KEY}` or `{BRAVE_API_KEY}` placeholders, rendered from the environment at send time; a plan containing a secret's value cannot be built. `identity` names the key or address a request carries (`TEXT_PLAGIARISM_MAILTO` appears only in the Crossref User-Agent).

## Who sees what

| Service | Receives | Identity |
|---|---|---|
| api.crossref.org | reference-list strings | `TEXT_PLAGIARISM_MAILTO` in the User-Agent, only if set |
| api.openalex.org | DOIs (strict); approved phrases of about 12 words (open) | `OPENALEX_API_KEY` only if set (strict) or `--use-keys` (open) |
| zenodo.org | record ids | none |
| arXiv, Europe PMC, publisher and repository hosts | the public URL of an open copy | none |
| Europe PMC search | approved phrases (open) | none |
| CORE, Open Library | approved phrases (open, opt-in backends) | `CORE_API_KEY` only with `--use-keys` |
| Brave | approved phrases (open, opt-in, key required) | `BRAVE_API_KEY` always |

Redirects are followed only for GET requests that carry no user text; a redirect on a `user_text` request is reported as `redirect_blocked`.

## What stays on disk

Under the cache root (`TEXT_PLAGIARISM_CACHE`, else `%LOCALAPPDATA%/text-plagiarism` on Windows, else `$XDG_CACHE_HOME` or `~/.cache`, plus `/text-plagiarism`) and never inside the skill or the repository: run directories (`runs/<hash>/`, which hold the extracted text of the checked document, the plans, `requests.jsonl` and the state), an HTTP cache of 200 responses, and extracted text of downloaded open-access works. The report carries only matched passages, DOI or URL, license and version. Downloaded full text is not re-shared.

## Disclosure

`requests.jsonl` logs every request (service, tier, round, templated URL, `text_sent`, identity, status, error, final URL, whether it came from the cache). The report's `disclosure` block lists all of them.

## The limits of the guarantee

The scripts cannot tell the user from Claude: approval is relayed by Claude and gated by Claude Code's permission prompt on each `run` command. The guarantee covers only requests made by the skill's scripts. Text that Claude reads (the document, a report) is already in the model's context.
