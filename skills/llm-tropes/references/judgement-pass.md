# Judgement pass

Optional second pass for tropes that no regex can catch. Use the catalog's
`explanation`/`example` fields as the definition. This pass finds patterns for a
human to review. It is not evidence of who wrote the text.

## Rules

- Judge only sections the scan marked `dense: true`, or a small sample the user agreed to. Never judge other sections.
- Quote the exact line and give its line number from the scan JSON.
- Label every finding `judgement`, so it stays distinct from `regex` findings.
- Report at most about 10 judgement findings per document. Keep the strongest.
- When unsure, leave it out. A missed trope costs less than a false accusation.
- Never turn judgement findings into a score, probability or authorship claim.

## Tropes

### false-vulnerability (rhetorical)
A staged confession or polished "honest" admission that exists to build trust rather than convey a fact. The admitted flaw is usually vague, tidy and quickly turned into a lesson.
Example: "I'll be honest, I almost gave up."
Not a trope when: the admission is specific, costs the writer something, and the text then deals with it (a real limitations section, a postmortem).

### false-exclusivity (rhetorical)
Promises of hidden knowledge ("what nobody tells you") attached to material that is widely known.
Example: "What nobody mentions is the cost."
Not a trope when: the text really does report something unpublished or new, and says where it comes from.

### invented-concept-labels (rhetorical)
A coined, named phenomenon presented as if established, such as "the acceleration trap", with no citation and no definition beyond the label.
Example: "the acceleration trap"
Not a trope when: the term is defined, cited, or introduced as the author's own proposal (new terms in a paper's contribution).

### stakes-inflation (rhetorical)
Ordinary topics framed as world-changing. Look for sweeping verbs with no evidence of scale. The regex catches fixed phrases; judge the paraphrases.
Example: "This will fundamentally reshape how we work."
Not a trope when: the claim is quantified or backed by a source, or the field really is shifting and the text shows it.

### one-point-dilution (document)
One idea restated across many paragraphs with new wording but no new content.
Example: "The same point in ten paragraphs."
Not a trope when: repetition serves teaching (an abstract, then the body, then a conclusion) or each restatement adds a case, number or caveat.

### historical-analogy-stacking (document)
A chain of historical comparisons used to lend weight, each shallow and none examined.
Example: "Like the printing press, like the railway..."
Not a trope when: one analogy is developed and its limits are discussed, as in a history-of-science argument.

### tricolon-abuse and anaphora-abuse (structural)
Rule-of-three lists, and runs of sentences opening with the same words, used reflexively and everywhere. Judge frequency and rhythm across the section, not a single instance.
Example: "identity, payments, compute"; "They could... They could..."
Not a trope when: it appears once or twice for emphasis, or the list really has three items (a method with three steps, a legal enumeration).

### compliment-sandwich (rhetorical)
Praise, mild criticism, praise, in feedback-style text, with generic praise on both sides.
Example: "Great start! However... Overall excellent."
Not a trope when: the praise is specific and the criticism is actionable.

### fake-casual-quotes (rhetorical)
Invented quotations in a casual register ("Honestly? It just works.") that no source is named for.
Not a trope when: the quote is attributed to a real, identifiable speaker or is the writer's own dialogue in fiction.

### fractal-summaries (document)
Every section, and the whole document, ends by summarising itself at several nested levels, so the reader meets the same summary repeatedly.
Not a trope when: the genre requires it (abstract plus conclusion, executive summary).

## Output format for each finding

`judgement | line N | "exact quoted line" | trope id | one-sentence reason`
