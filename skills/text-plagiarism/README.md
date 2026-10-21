# text-plagiarism (under development)

Checks a document (PDF, Markdown, Word, plain text) for passages copied verbatim from published papers and books, and reports for each finding the source, its license and how the passage is attributed (quoted and cited, cited only, or unattributed). It reports evidence only and always states what could not be checked.

## Status

Scaffold only: planned, not started. This folder is hidden from normal installs until the skill is finished.

## Planned behaviour

- **Strict tier (default):** resolves your reference list and compares your text locally against the open-access full text of the works you cite.
- **Open tier (opt-in):** after you choose which sections may be searched and approve the exact phrases, queries open scholarly indexes such as Europe PMC and OpenAlex.
- **Optional reference corpus:** compare against papers you provide (local PDFs or DOIs); local files never leave your machine.
- Paywalled and unpublished sources cannot be checked unless you supply them; the report says so instead of reporting "clean".

## License

Apache-2.0 (see the repository `LICENSE`).
