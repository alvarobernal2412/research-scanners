# Attribution

The pattern catalog is a set of descriptive signals, not proof of AI authorship.

Convention: each pattern's `source` field is `<Name> — <detail/URL>`, several joined by `; `. The text before ` — ` is the short source name, and every short name must appear in this file (checked by `tests/test_catalog.py`).

## Wikipedia (CC BY-SA 4.0)

Entries in `patterns_wikipedia.json` are derived from the Wikipedia page "Signs of AI writing" (https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing), whose text is available under Creative Commons Attribution-ShareAlike 4.0 (https://creativecommons.org/licenses/by-sa/4.0/). Patterns were adapted: condensed, converted to regexes, reweighted. `patterns_wikipedia.json` is therefore licensed CC BY-SA 4.0 (share-alike); the skill's code is a separate work. Loading the catalog without that file excludes all CC BY-SA material.

## MIT-licensed sources

Used with the MIT notice retained: "Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the Software), to deal in the Software without restriction, subject to the inclusion of the copyright notice and this permission notice in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED AS IS, WITHOUT WARRANTY OF ANY KIND."

- **FeedHive** - https://github.com/FeedHive/ai-slop-detector (MIT), copyright its authors. Category ideas and chatbot-residue signals.
- **wsc** - MIT-licensed deterministic rule detectors for AI-writing signals; rule-category ideas, copyright its authors.
- **blader/humanizer** - https://github.com/blader/humanizer (MIT), copyright its authors. Phrase-level signals.
- **Aboudjem** - https://github.com/Aboudjem/humanizer-skill (MIT), copyright its authors. Weighted-metrics idea (burstiness, MATTR, trigram repetition) and filler phrasing. Its P1-P30 list is itself CC BY-SA derived from Wikipedia and is not copied here.

## Other

- **tropes.fyi** - https://gist.github.com/ossa-ma/f3baa9d25154c33095e22272c631f5a1 (no license stated). Inspiration only: every trope is rewritten in our own wording, with our own regexes and examples; no text copied.
- **Kobak et al.** - "Delving into LLM-assisted writing in biomedical publications through excess vocabulary", https://arxiv.org/abs/2406.07016 (Science Advances 11(27) eadt3813). Source of the excess-vocabulary idea; only individual words are listed.
