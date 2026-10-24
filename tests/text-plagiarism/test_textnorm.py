from extract import Line
from textnorm import glued_ratio, normalise_word, split_sentences, tokenize, GLUED_MAX_RATIO


def L(*texts, start=1):
    return [Line(start + i, t) for i, t in enumerate(texts)]


def keys(lines):
    return [t.key for t in tokenize(lines)]


def test_line_end_hyphen_joined():
    assert keys(L("see previ-", "ous work")) == ["see", "previous", "work"]


def test_compound_hyphen_matches_joined_form():
    assert keys(L("a non-linear model")) == ["a", "nonlinear", "model"]
    assert keys(L("a non-", "linear model")) == ["a", "nonlinear", "model"]


def test_lone_spaced_hyphen_between_lowercase_words_is_joined():
    assert keys(L("data - driven approach")) == ["datadriven", "approach"]
    assert keys(L("Wait - Now go")) == ["wait", "now", "go"]


def test_curly_quotes_ligatures_case_normalised():
    assert keys(L("“Don’t ﬁnd oﬀ THEM,”")) == ["don't", "find", "off", "them"]
    assert normalise_word("soft­hyphen") == "softhyphen"
    assert normalise_word("(2020).") == "2020"


def test_token_columns_map_back_to_text():
    lines = L("alpha beta", "gamma delta-", "epsilon zeta")
    by_no = {l.no: l.text for l in lines}
    toks = tokenize(lines)
    assert toks[0].line == 1 and by_no[1][toks[0].start:toks[0].end] == "alpha"
    assert by_no[2][toks[2].start:toks[2].end] == "gamma"
    merged = toks[3]
    assert merged.key == "deltaepsilon" and merged.line == 2 and merged.end_line == 3
    assert by_no[2][merged.start:] == "delta-" and by_no[3][:merged.end] == "epsilon"


def test_sentence_split_guards_abbreviations():
    text = ("We follow Smith et al. Jones (e.g. in Fig. 3) and J. Doe. This is a second sentence! "
            "Is it? Yes it is.")
    lines = L(text)
    sents = split_sentences(lines, tokenize(lines))
    assert [s.text.split()[0] for s in sents] == ["We", "This", "Is", "Yes"]
    assert sents[0].text.endswith("J. Doe.")


def test_sentence_split_blank_line_and_headings():
    lines = [Line(1, "Intro", heading_level=1), Line(2, "First part"), Line(3, "continues here."),
             Line(4, ""), Line(5, "New para without period"), Line(6, "More.")]
    sents = split_sentences(lines, tokenize(lines))
    assert [s.text for s in sents] == ["Intro", "First part continues here.", "New para without period More."]
    assert sents[1].line_start == 2 and sents[1].line_end == 3


def test_glued_ratio_detects_glued_text():
    glued = "Forcompleteness,Table1reproducesthefollowingresultsinfulldetailaboutthemethodsusedhere and more words"
    toks = tokenize(L(glued * 3))
    assert glued_ratio(toks) > GLUED_MAX_RATIO
    normal = tokenize(L("The quick brown fox jumps over the lazy dog while telecommunication remains fine"))
    assert glued_ratio(normal) <= GLUED_MAX_RATIO
    assert glued_ratio([]) == 0.0
