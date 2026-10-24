import random

from compare import (MIN_MATCH_WORDS, SHINGLE_K, WINNOW_W, SourceIndex, _hash, find_matches, fingerprints)
from extract import Line
from textnorm import tokenize

T1 = ("The proposed conceptual model provides a new understanding of analytic provenance by formalizing the "
      "highly iterative and non-linear realities of exploratory process mining.")


def K(text):
    return [t.key for t in tokenize([Line(1, text)])]


def filler(n, seed):
    rnd = random.Random(seed)
    return [f"w{rnd.randrange(10**6)}x" for _ in range(n)]


def test_fingerprints_stable_across_processes():
    assert _hash(["a", "b", "c", "d", "e"]) == 13016736390431212562


def test_winnowing_guarantee():
    run_len = WINNOW_W + SHINGLE_K - 1
    for seed in range(40):
        shared = filler(run_len, 1000 + seed)
        a = filler(seed % 7 + 3, seed) + shared + filler(9, seed + 500)
        b = filler(11, seed + 900) + shared + filler(seed % 5 + 2, seed + 700)
        assert set(fingerprints(a)) & set(fingerprints(b)), seed


def test_verbatim_match_reports_extent():
    doc = filler(20, 1) + K(T1) + filler(15, 2)
    src = filler(30, 3) + K(T1) + filler(5, 4)
    ms = find_matches(doc, src)
    assert len(ms) == 1
    m = ms[0]
    assert m.kind == "verbatim" and m.matched_words == 23 and m.longest_run == 23
    assert (m.doc_start, m.doc_end) == (20, 43) and (m.src_start, m.src_end) == (30, 53)


def test_near_verbatim_merges_small_gap():
    changed = T1.replace("analytic", "forensic")
    doc = filler(10, 1) + K(changed) + filler(10, 2)
    src = filler(10, 3) + K(T1) + filler(10, 4)
    ms = find_matches(doc, src)
    assert len(ms) == 1 and ms[0].kind == "near_verbatim"
    assert ms[0].matched_words == 22 and ms[0].longest_run >= 11


def test_far_apart_runs_not_merged():
    a, b = K(T1), K("Entirely different statement concerning ontologies and their applications in biomedical informatics research")
    doc = a + filler(6, 5) + b
    src = a + filler(9, 6) + b
    ms = find_matches(doc, src)
    assert len(ms) == 2 and all(m.kind == "verbatim" for m in ms)


def test_short_or_stopword_matches_dropped():
    eleven = K("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda")
    assert len(eleven) == 11
    assert find_matches(filler(5, 1) + eleven + filler(5, 2), filler(7, 3) + eleven + filler(2, 4)) == []
    stops = ["the", "of", "and", "to", "a", "in", "that", "is", "for", "it", "with", "as", "was", "on"]
    stops = stops[:12] + ["model", "data"]  # 14 words, only 2 content words
    assert len(stops) == 14
    assert find_matches(filler(5, 5) + stops + filler(5, 6), filler(5, 7) + stops + filler(5, 8)) == []
    assert MIN_MATCH_WORDS == 12


def test_min_words_override_for_verification():
    eight = K("alpha beta gamma delta epsilon zeta eta theta")
    d, s = filler(5, 1) + eight + filler(5, 2), filler(7, 3) + eight + filler(2, 4)
    assert find_matches(d, s) == []
    assert len(find_matches(d, s, min_words=8)) == 1


def test_matches_across_pdf_hyphenation():
    src = tokenize([Line(1, "we study exploratory pro-"), Line(2, "cess mining on event logs in many settings today")])
    doc = tokenize([Line(1, "we study exploratory process mining on event logs in many settings today")])
    ms = find_matches([t.key for t in doc], [t.key for t in src])
    assert len(ms) == 1 and ms[0].matched_words == 12


def test_source_index_matches_pairwise_and_is_fast():
    srcs = {f"s{i}": filler(300, 100 + i) for i in range(20)}
    doc = filler(50, 9) + K(T1) + filler(50, 10)
    srcs["s7"] = filler(40, 11) + K(T1) + filler(40, 12)
    idx = SourceIndex()
    for sid, keys in srcs.items():
        idx.add(sid, keys)
    res = idx.find(doc)
    assert list(res) == ["s7"] and res["s7"][0].matched_words == 23
    assert res["s7"] == find_matches(doc, srcs["s7"])


def test_spike_sentences_match_recorded_pdf_text():
    import base64
    import json

    from conftest import FIXTURES
    from extract import extract_bytes

    rec = json.loads((FIXTURES / "responses" / "pdf-A.json").read_text(encoding="utf-8"))
    src_doc = extract_bytes(base64.b64decode(rec["body_b64"]), "pdf")
    src = [t.key for t in tokenize(src_doc.lines)]
    p = json.loads((FIXTURES / "passages.json").read_text(encoding="utf-8"))["workA"]
    for sentence in (p["control_sentence"], p["miss_sentence"], p["passage"]):
        doc = filler(8, 1) + K(sentence) + filler(8, 2)
        ms = find_matches(doc, src)
        assert ms and max(m.longest_run for m in ms) >= len(K(sentence)) - 1, sentence
