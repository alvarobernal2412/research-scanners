# code-plagiarism (under development)

Checks a codebase for snippets copied from Stack Overflow and open-source projects, and reports for each finding the candidate origins, the license evidence, the matched lines and whether attribution already exists. It reports evidence only: no legal verdict, and no claim about the direction of copying.

## Status

Scaffold only. Work in progress lives on the branch `feat/code-plagiarism`; this folder is hidden from normal installs until the skill is finished.

## Planned behaviour

- **Strict tier (default):** reads the project's declared dependencies and looks up their licenses by public package coordinates. Dependencies that clearly come from a private registry are not sent.
- **Fingerprint tier (opt-in):** uploads a sanitised SCANOSS fingerprint file (neutral file names, sizes set to 0, hashes only) after you approve the exact request.
- **Open tier (opt-in):** queries Stack Exchange with a few identifiers per code block, after you approve the exact requests.
- Every networked step uses a `plan` then approval then `run` gate: nothing is sent that is not in the plan you approved.

## License

Apache-2.0 (see the repository `LICENSE`).
