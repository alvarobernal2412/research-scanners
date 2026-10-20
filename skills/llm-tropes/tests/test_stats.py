import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from extract import Line  # noqa: E402
from stats import compute_stats  # noqa: E402

KEYS = {
    "words",
    "sentence_length_variance",
    "mattr",
    "trigram_repetition",
    "tricolon_per_1k",
    "em_dash_per_1k",
    "bold_first_bullet_ratio",
}


def mk(*texts):
    return [Line(i + 1, t, False, False) for i, t in enumerate(texts)]


def test_empty_text_gives_zeros_not_errors():
    for lines in ([], mk(""), mk("   ")):
        s = compute_stats(lines)
        assert set(s) == KEYS
        assert all(v == 0.0 and isinstance(v, float) for v in s.values())


def test_uniform_sentences_have_zero_variance():
    s = compute_stats(mk("One two three. Four five six. Seven eight nine."))
    assert s["sentence_length_variance"] == 0.0
    assert s["words"] == 9.0


def test_mattr_lower_for_repetitive_text():
    rep = compute_stats(mk("the cat sat " * 40))
    div = compute_stats(mk(" ".join(f"word{i}" for i in range(120))))
    assert rep["mattr"] < div["mattr"]
    assert div["mattr"] == 1.0


def test_trigram_repetition_detects_repeated_phrase():
    rep = compute_stats(mk("in order to win, in order to win, in order to win"))
    none = compute_stats(mk("alpha beta gamma delta epsilon zeta eta"))
    assert rep["trigram_repetition"] > 0.5
    assert none["trigram_repetition"] == 0.0


def test_em_dash_rate_per_1k_words():
    text = " ".join(["word"] * 99) + " — x —"
    s = compute_stats(mk(text))
    assert s["words"] == 100.0
    assert s["em_dash_per_1k"] == 20.0


def test_bold_first_bullet_ratio():
    lines = mk("- **Bold** one", "* **Bold** two", "+ plain three", "1. **Bold** four", "text")
    assert compute_stats(lines)["bold_first_bullet_ratio"] == 0.75
