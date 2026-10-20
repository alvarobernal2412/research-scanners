---
name: llm-tropes
description: Scans a text, draft, paper section, README or post for "AI writing" tells, LLM tropes and slop (when asked whether it "sounds like ChatGPT" or has "AI tells"), and reports matched lines with sources and tell density. Reports evidence only and gives no verdict on who wrote the text.
---

# llm-tropes

Finds stylistic tells commonly associated with LLM output and reports them with line numbers and sources.

## When to use

- The user asks to check, scan or audit a document for AI writing, LLM tropes, slop, or "sounding like ChatGPT".

Do not use for: rewriting or humanising text (offer it separately), deciding whether an AI wrote something (this is not an AI detector), or plagiarism checks.

## Run the scan

Install once: `pip install -r <skill dir>/requirements.txt`.

```
python -I <skill dir>/scripts/scan.py <file> [--profile general|academic|technical|marketing]
```

`<skill dir>` is the directory this SKILL.md was loaded from. Formats: md, txt, docx, pdf. Pick `academic` for papers, `technical` for READMEs and docs, `marketing` for promo copy, else `general`. Profiles currently only filter catalog entries that declare a `profiles` list, and none do yet, so all profiles behave the same today.

## Read the JSON

- `error`: `file_not_found`, `unsupported_format`, `unreadable_file`, `catalog_error` (broken pattern catalog, see `detail`), or `no_text_extracted` (empty input, or usually a scanned PDF that needs OCR). Tell the user and stop.
- `findings`: `line`, `text`, `pattern_id`, `category`, `source`, `weight`, `contested`. Contested findings (for example the em dash) are low weight: they are listed but excluded from the density figure and from the category count.
- `sections`: `dense: true` means three or more categories in one section and a high weighted density there (`density_per_1k` of the section against `DENSE_MIN_PER_1K` in `scan.py`). These are the places worth attention. Text without Markdown headings is one section.
- `density_per_1k`: weighted non-contested hits per 1,000 words. `stats`: sentence and vocabulary statistics.
- `language_note`: set when the text does not look English. The patterns are English, so say results are unreliable.

## Optional judgement pass

Offer it only when dense sections exist. Read `references/judgement-pass.md` and apply it to dense sections or an agreed sample only. Label those findings `judgement`. They are reading aids, never evidence of authorship.

## Present the report

Markdown, grouped by section heading. For each section a table:

| Line | Matched text | Trope | Category | Source |
|---|---|---|---|---|

Link the source when it is a URL. Then give a short density summary (`density_per_1k`, dense sections, notable stats), list `language_note` if set, and end with the `disclaimer` text from the JSON, verbatim.

Never output an "AI probability", a percentage likelihood, a score of how AI-written it is, or any verdict about who wrote the text.

## Network

The skill makes no network calls. The one exception is the optional `scripts/check_dois.py`, which sends only DOIs to api.crossref.org to check that cited references resolve. Offer it only for texts with references, and run it only if the user agrees. `--offline` guarantees no requests. This guarantee covers requests made by the skill's scripts; text Claude reads is already in the model's context. DOIs that contain `<` or `>` (for example some AMS/SICI DOIs) are truncated by the extractor and may come back `not_found` falsely.

## Caveats to state

- The signs are descriptive. Humans write them too, and good LLM output may show none.
- AI-text detectors are biased against non-native English writers (Liang et al., https://arxiv.org/abs/2304.02819). Do not use this report to accuse anyone.
- The em dash is contested: common in human writing, so it is low weight.

## Evaluation

`python -I <skill dir>/scripts/evaluate.py <cases.json>` scores the scanner against human-labelled cases (precision and recall per category). Maintainer tool; not part of a normal scan.

## Attribution

Pattern sources and licences are in `data/ATTRIBUTION.md`.
