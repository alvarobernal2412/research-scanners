"""Runtime content guard: a strict request never carries a 5-word window of the user's own body text."""
import json
import urllib.parse

import pytest

import evaluate
import prepare
import strict
from approval import PlannedRequest, body_windows, build_plan, content_violation
from helpers import call_main, write_md
from net import replay_opener
from conftest import FIXTURES

SECRET = "Our secret method computes the frobnication index of each trace with the novel zeta kernel."
THESIS = ("1 Introduction\n\nProcess mining analyses event logs recorded by information systems.\n\n"
          "References\n\n[1] W. van der Aalst. Process Mining: Data Science in Action. Springer, 2016.\n"
          "[2] A. Smith. Some paper title about logs. Journal of Logs, 2019.\n\n"
          "2 Methods\n\n" + SECRET + "\n\nWe then evaluate the kernel on confidential hospital data.\n")


def _req(url, carries="reference_string"):
    return PlannedRequest(id="", service="crossref", method="GET", url=url, carries=carries, purpose="p")


def test_chapter_after_references_is_never_sent(tmp_path):
    doc = write_md(tmp_path, "thesis.txt", THESIS)
    wd = tmp_path / "wd"
    code, summary = call_main(prepare.main, [str(doc), "--workdir", str(wd)])
    assert code == 0
    code, plan = call_main(strict.main, ["plan", "--workdir", str(wd)])
    assert code == 0 and not plan["done"]
    blob = urllib.parse.unquote_plus(open(plan["plan"], encoding="utf-8").read()).lower()
    assert "frobnication" not in blob and "zeta kernel" not in blob and "confidential hospital" not in blob
    assert "aalst" in blob


def test_build_plan_refuses_body_window_in_payload():
    windows = body_windows([SECRET])
    leak = _req("https://api.crossref.org/works?query.bibliographic=" +
                urllib.parse.quote("computes the frobnication index of each trace"))
    with pytest.raises(ValueError, match="body text"):
        build_plan("strict", "resolve", "d" * 64, [leak], body_windows=windows)
    ok = _req("https://api.crossref.org/works?query.bibliographic=" + urllib.parse.quote("W. van der Aalst. Process Mining"))
    assert build_plan("strict", "resolve", "d" * 64, [ok], body_windows=windows)["requests"]
    assert content_violation(leak, windows) is not None and content_violation(ok, windows) is None
    # four shared words are not enough
    four = _req("https://x/?q=" + urllib.parse.quote("computes the frobnication index"))
    assert content_violation(four, windows) is None


def test_reference_entry_copying_body_text_is_dropped_from_plan(tmp_path):
    text = ("# Introduction\n\n" + SECRET + "\n\n# References\n\n[1] W. van der Aalst. Process Mining. Springer, 2016.\n"
            "[2] " + SECRET + "\n")
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(write_md(tmp_path, "d.md", text)), "--workdir", str(wd)])
    code, plan = call_main(strict.main, ["plan", "--workdir", str(wd)])
    blob = urllib.parse.unquote_plus(open(plan["plan"], encoding="utf-8").read()).lower()
    assert "frobnication" not in blob and "aalst" in blob
    assert plan["preview"]["refused"], plan


def test_run_refuses_hand_edited_plan_with_body_text(tmp_path):
    doc = write_md(tmp_path, "thesis.txt", THESIS)
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(doc), "--workdir", str(wd)])
    code, plan = call_main(strict.main, ["plan", "--workdir", str(wd)])
    data = json.loads(open(plan["plan"], encoding="utf-8").read())
    data["requests"][0]["url"] += urllib.parse.quote(" " + SECRET)
    import hashlib
    from approval import write_plan
    sha = write_plan(data, __import__("pathlib").Path(plan["plan"]))
    code, out = call_main(strict.main, ["run", "--workdir", str(wd), "--plan", plan["plan"], "--sha256", sha],
                          opener=replay_opener(FIXTURES / "responses"))
    assert code == 2 and out["error"] == "plan_leaks_body_text"
    assert not (wd / "requests.jsonl").exists()


def test_evaluate_privacy_gate_checks_content_not_label(tmp_path):
    doc = write_md(tmp_path, "thesis.txt", THESIS)
    wd = tmp_path / "wd"
    call_main(prepare.main, [str(doc), "--workdir", str(wd)])
    clean = [{"tier": "strict", "carries": "reference_string", "url": "https://x/?q=Process+Mining", "text_sent": None}]
    leaky = [{"tier": "strict", "carries": "reference_string",
              "url": "https://api.crossref.org/works?query.bibliographic=" + urllib.parse.quote(SECRET),
              "text_sent": None}]
    assert evaluate.content_leaks(wd, clean) is False
    assert evaluate.content_leaks(wd, leaky) is True
