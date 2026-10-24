# Fixture provenance

Everything here is replayed by the tests; no test touches the network. Recorded on 2026-10-24 with
`record.py` (only public identifiers and public CC BY sentences from the works below were sent), except where
noted. Attribution for the CC BY 4.0 works, reproduced as test fixtures with no changes to the text:

| Work | DOI | Authors | License | Used for |
|---|---|---|---|---|
| Robotic Process Mining: Vision and Challenges, Business & Information Systems Engineering 63(3), 2021 (work A) | 10.1007/s12599-020-00641-4 | Volodymyr Leno, Artem Polyvyanyy, Marlon Dumas, Marcello La Rosa, Fabrizio Maria Maggi | CC BY 4.0 (OpenAlex location license `cc-by`, text of the PDF) | PDF full text (strict tier positive), OpenAlex window search hit |
| Identifying Frequent Health Care Users and Care Consumption Patterns: Process Mining of Emergency Medical Services Data, JMIR 23(10), 2021 (work D, PMC8529480) | 10.2196/27499 | Laura Maruster, Durk-Jouke van der Zee, Erik Buskens | CC BY 4.0 (Europe PMC `license: cc by`, JATS `ali:license_ref`) | Europe PMC search hit and JATS full text (open tier positive control) |
| Process mining framework with time perspective for understanding acute care: a case study of AIS in hospitals, BMC Med Inform Decis Mak, 2021 (work C, PMC8684667) | 10.1186/s12911-021-01725-1 | Pang, Xu, Ren, Yang, Li, Lu, Zhao | CC BY 4.0 | one sentence with an apostrophe (Europe PMC search responses only) |

Retrieval date 2026-10-24 for all live recordings. Passages used: `passages.json` (`workA.passage`,
`workA.control_sentence`, `pmc.sentences`, `apostrophe.sentence`). The sentences are quoted verbatim from the CC BY
texts for testing under their license.

## Evaluation cases

`cases/` holds the documents and corpus files of `scripts/evaluate.py`. They are generated from `passages.json`
(invented surrounding text plus the CC BY sentences above). `cases/corpus_related/work-a-excerpt.txt` is a short
excerpt of work A (the control sentence and the passage). `cases/corpus_unrelated/` holds invented text and
`cases/corpus_own/` invented text plus the work A passage for the self-overlap case. `T8_scanned.pdf` is a PDF
page with no text. The real T2 sentence appears nowhere; `cases/T2.md` uses the invented stand-in.

## Notes on what is real and what is not

- `responses/pdf-A.json`: the Springer PDF of work A. Springer now answers plain HTTP clients with a bot-challenge
  page (kept as `responses/pdf-A-challenge.json`, recorded live, not in `index.json`; it is the "download blocked"
  case). The PDF itself is the copy fetched from the same URL on 2026-10-20 during the fixtures spike (HTTP 200,
  614,397 bytes) and registered under that URL in `index.json`.
- `responses/oa-work-T1.json`, `zenodo-21373046.json` (a 302 to `21373047`, `final_url` recorded) and
  `zenodo-21373047.json`: the public records of the paywalled chapter with DOI 10.1007/978-3-032-40795-5_31
  (acceptance case T1). The Zenodo record holds only an appendix, a figure and a spreadsheet. No text of the chapter
  was sent anywhere and none is stored.
- `passages.json` `t1.sentence` is the user's own published sentence (allowed in fixtures). `t2_standin.sentence`
  is an invented stand-in; the real T2 sentence is neither stored nor searched.
- No response exists for the T1 or T5 sentences: they were not sent. Tests that need zero-hit search responses use
  the recorded misses (`epmc-search-A1`, `oa-fulltext-A1-w12`, `oa-fulltext-D1-w12`, `core-*`); tests that need many-hit
  bodies or 429/500 build them in the test and mark them synthetic there. The Brave response shape is synthetic
  (no key was available, so no recording exists).
- CORE answered `totalHits: 0` for both sentences (consistent with the spike: body-text coverage unverified).
- The OpenAlex `fulltext.search` responses are stemmed phrase searches; the A2 window hit and the A1/D1 misses match
  the spike table (`docs/research/2026-10-20-text-fixtures-spike.md`).
