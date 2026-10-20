# Attribution

The pattern catalog is a set of descriptive signals, not proof of AI authorship.

Convention: each pattern's `source` field is `<Name> — <detail/URL>`, several joined by `; `. The text before ` — ` is the short source name, and every short name must appear in this file (checked by `tests/test_catalog.py`).

## Wikipedia (CC BY-SA 4.0)

Entries in `patterns_wikipedia.json` are derived from the Wikipedia page "Signs of AI writing" (https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing), whose text is available under Creative Commons Attribution-ShareAlike 4.0 (https://creativecommons.org/licenses/by-sa/4.0/). Patterns were adapted: condensed, converted to regexes, reweighted. `patterns_wikipedia.json` is therefore licensed CC BY-SA 4.0 (share-alike); the skill's code is a separate work. Loading the catalog without that file excludes all CC BY-SA material.

## MIT-licensed sources

License and copyright lines verified from each repository's LICENSE file on 2026-10-20. Entries credited to these sources are reworded ideas (categories, phrase types, metrics), not copied text; the notices are retained anyway.

- **FeedHive** - https://github.com/FeedHive/ai-slop-detector - MIT License, "Copyright (c) 2026 FeedHive".
- **wsc** - https://github.com/theserverlessdev/wsc - MIT License, "Copyright (c) 2025-2026 Ankur Singh".
- **blader/humanizer** - https://github.com/blader/humanizer - MIT License, "Copyright (c) 2025 Siqi Chen".
- **Aboudjem** - https://github.com/Aboudjem/humanizer-skill - MIT License, "Copyright (c) 2026 Adam Boudjemaa". Its P1-P30 list is itself CC BY-SA derived from Wikipedia, so any entry crediting Aboudjem together with Wikipedia lives in `patterns_wikipedia.json`.

MIT permission notice (applies to each of the four copyright holders above):

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Other

- **tropes.fyi** - https://gist.github.com/ossa-ma/f3baa9d25154c33095e22272c631f5a1 (no license stated). Inspiration only: every trope is rewritten in our own wording, with our own regexes and examples; no text copied.
- **Kobak et al.** - "Delving into LLM-assisted writing in biomedical publications through excess vocabulary", https://arxiv.org/abs/2406.07016 (Science Advances 11(27) eadt3813). Source of the excess-vocabulary idea; only individual words are listed.
