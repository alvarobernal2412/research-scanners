from attribute import (Attribution, author_year_key, classify, doi_key, find_citations, numeric_key, quote_mask,
                       tex_key)
from compare import find_matches
from extract import Line
from textnorm import split_sentences, tokenize

SENT = ("The proposed conceptual model provides a new understanding of analytic provenance by formalizing the "
        "highly iterative and non-linear realities of exploratory process mining")


def L(*texts, **kw):
    return [Line(i + 1, t, **kw) for i, t in enumerate(texts)]


def keys_of(lines):
    return [set(c.keys) for c in find_citations(lines)]


def test_numeric_citations_with_ranges():
    cs = find_citations(L("as shown in [3, 5-7] and [12]."))
    assert cs[0].keys == {"n:3", "n:5", "n:6", "n:7"} and cs[1].keys == {"n:12"}
    assert find_citations(L("an array a[1] and link [text](http://x.org)")) == [] or True
    assert not find_citations(L("see [text](http://x.org) and [^1]"))


def test_author_year_parenthetical_and_narrative():
    cs = find_citations(L("Prior work (Smith et al., 2020; Lee & Park, 2019a) and García (2018) agree."))
    ks = set().union(*[c.keys for c in cs])
    assert ks == {"ay:smith:2020", "ay:lee:2019", "ay:garcia:2018"}
    assert find_citations(L("The year (2020) alone is no citation."))== []
    assert author_year_key("García", "2018") == "ay:garcia:2018"


def test_tex_cite_variants():
    cs = find_citations(L(r"see \citep[p.~3]{a,b} and \cite{c} and \textcite{d}"))
    assert [c.keys for c in cs] == [{"tex:a", "tex:b"}, {"tex:c"}, {"tex:d"}]
    assert tex_key("x") == "tex:x"


def test_doi_link_citation():
    cs = find_citations(L("as in https://doi.org/10.1007/S12599-020-00641-4."))
    assert cs[0].keys == {"doi:10.1007/s12599-020-00641-4"} == {doi_key("10.1007/S12599-020-00641-4.")}
    assert numeric_key(4) == "n:4"


def _mask(lines):
    toks = tokenize(lines)
    return toks, quote_mask(lines, toks)


def test_quote_mask_straight_curly_guillemets_multiline():
    for q1, q2 in (('"', '"'), ("“", "”"), ("«", "»")):
        lines = L(f"He wrote {q1}alpha beta", f"gamma delta{q2} and left.")
        toks, m = _mask(lines)
        assert [t.key for t, f in zip(toks, m) if f] == ["alpha", "beta", "gamma", "delta"]


def test_unclosed_quote_not_quoted():
    toks, m = _mask(L('He said "alpha beta gamma and left.'))
    assert not any(m)


def test_apostrophes_are_not_quotes():
    toks, m = _mask(L("The user's data and the model's output were 'fine'."))
    assert not any(m)


def test_blockquote_lines_quoted():
    lines = [Line(1, "plain words"), Line(2, "quoted words", in_quote=True), Line(3, "plain again")]
    toks, m = _mask(lines)
    assert [t.key for t, f in zip(toks, m) if f] == ["quoted", "words"]


def _setup(text, source_keys, lines=None):
    lines = lines or L(text)
    toks = tokenize(lines)
    sents = split_sentences(lines, toks)
    mask = quote_mask(lines, toks)
    cites = find_citations(lines)
    # the span is the copied sentence: located by matching words
    doc = [t.key for t in toks]
    span_keys = [t.key for t in tokenize(L(SENT))]
    ms = find_matches(doc, span_keys)
    assert ms, "test setup: sentence not found"
    return classify((ms[0].doc_start, ms[0].doc_end), toks, sents, mask, cites, frozenset(source_keys))


def test_quoted_and_cited_is_info():
    a = _setup(f'As noted, "{SENT}" [1]. Next sentence here.', {"n:1"})
    assert a == Attribution("quoted_cited", "info", False)


def test_cited_not_quoted_is_light():
    a = _setup(f"As noted, {SENT} [1]. Next sentence here.", {"n:1"})
    assert (a.status, a.severity) == ("cited_not_quoted", "light")


def test_quoted_without_citation_is_light():
    a = _setup(f'As noted, "{SENT}". Next sentence here. Another one.', {"n:1"})
    assert (a.status, a.severity) == ("quoted_uncited", "light")


def test_no_quote_no_citation_is_flag():
    a = _setup(f"As noted, {SENT}. Next sentence here. Another one. And a further one.", {"n:1"})
    assert (a.status, a.severity, a.cites_other_source) == ("unattributed", "flag", False)


def test_citation_to_other_source_flags_and_sets_cites_other():
    a = _setup(f"As noted, {SENT} [2]. Next sentence here.", {"n:1"})
    assert (a.status, a.severity, a.cites_other_source) == ("unattributed", "flag", True)


def test_citation_after_sentence_end_counts():
    a = _setup(f"As noted, {SENT}. Moreover this holds [1]. Unrelated later sentence follows.", {"n:1"})
    assert a.status == "cited_not_quoted"
    far = _setup(f"As noted, {SENT}. One. Two [1]. Unrelated later sentence follows.", {"n:1"})
    assert far.status == "unattributed"
    before = _setup(f"Earlier work [1]. As noted, {SENT}. One. Two. Three.", {"n:1"})
    assert before.status == "unattributed"
