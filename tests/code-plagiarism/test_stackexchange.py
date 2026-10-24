"""Open tier: Stack Exchange backend. Queries carry only rare identifiers; nothing else leaves."""
import hashlib
import io
import json
import re
import shutil
import sys
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"))

import approval  # noqa: E402
import stackexchange as se  # noqa: E402
from inventory import read_lines  # noqa: E402
from normalize import tokenize  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
CASES = FIXTURES / "cases"
RESPONSES = FIXTURES / "responses"


def answers_by_id():
    out = {}
    for name in ("se_answers_spike.json", "se_answers_53954986.json", "se_answers_noamopt.json"):
        for it in json.loads((RESPONSES / name).read_text(encoding="utf-8"))["items"]:
            out[it["answer_id"]] = it
    return out


ANSWERS = answers_by_id()


class Resp:
    def __init__(self, status, body, headers=None):
        self.status, self._body, self.headers = status, body, dict(headers or {})

    def read(self, n=-1):
        return self._body

    def getcode(self):
        return self.status

    def close(self):
        pass


class FakeSE:
    """urlopen-compatible fake of the three endpoints, answering from fixtures."""

    def __init__(self, search=None, revisions=None, quota=250, backoff=None, search_status=None,
                 search_body=None, has_more=False):
        self.search = search or (lambda q, page: [])
        self.revisions = revisions or {}
        self.quota, self.backoff, self.has_more = quota, backoff, has_more
        self.search_status, self.search_body = search_status, search_body
        self.urls = []

    def wrap(self, items, **extra):
        data = {"items": items, "has_more": self.has_more, "quota_max": 300, "quota_remaining": self.quota}
        if self.backoff is not None:
            data["backoff"] = self.backoff
        data.update(extra)
        return data

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        parts = urlsplit(url)
        qs = parse_qs(parts.query)
        if parts.path.endswith("/search/excerpts"):
            if self.search_status is not None:
                return Resp(self.search_status, json.dumps(self.search_body or {}).encode())
            q = unquote_plus(qs["q"][0])
            page = int(qs.get("page", ["1"])[0])
            ids = self.search(q, page)
            return Resp(200, json.dumps(self.wrap([{"item_type": "answer", "answer_id": i,
                                                    "question_id": 1} for i in ids])).encode())
        ids = [int(x) for x in parts.path.rsplit("/", 2)[-2 if parts.path.endswith("revisions") else -1].split(";")]
        if parts.path.endswith("/revisions"):
            items = [r for i in ids for r in self.revisions.get(i, [])]
            return Resp(200, json.dumps(self.wrap(items)).encode())
        items = [ANSWERS[i] for i in ids if i in ANSWERS]
        return Resp(200, json.dumps(self.wrap(items)).encode())


def mk(root, files):
    for rel, content in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return Path(root)


def case(tmp_path, *names):
    """Copy fixture cases into one project; returns the root."""
    root = tmp_path / "proj"
    for n in names:
        shutil.copytree(CASES / n, root / n)
    return root


def hit_c1(q, page):
    return [58437909] if any(w in q for w in ("URLSearchParams", "jsonplaceholder", "doAsyncTask")) else []


def plan_file(tmp_path, plan):
    p = tmp_path / "plan.json"
    approval.write_plan(plan, p)
    return p


def planned_text(plan):
    return "\n".join(r["url"] + "\n" + (r["body"] or "") for r in plan["requests"])


def toks(path):
    return tokenize(read_lines(Path(path)), "c_like")


# ---- identifiers --------------------------------------------------------------------

def test_rare_tokens_c1():
    t = toks(CASES / "C1" / "C1_so_2019_cc4.js")
    rare = se.rare_tokens(t, se.load_common())
    assert {"URLSearchParams", "jsonplaceholder.typicode.com", "doAsyncTask"} & set(rare)
    for common in ("const", "fetch", "then", "console"):
        assert common not in rare


def test_rare_tokens_survive_renaming_c10():
    qs = se.build_queries(toks(CASES / "C10" / "C10_renamed_of_C1.js"))
    assert qs and any("URLSearchParams" in q or "jsonplaceholder.typicode.com" in q for q in qs)


def test_queries_at_most_three_tokens():
    for f in (CASES / "C1" / "C1_so_2019_cc4.js", CASES / "C10" / "C10_renamed_of_C1.js"):
        qs = se.build_queries(toks(f))
        assert 1 <= len(qs) <= 3
        assert len(qs[0].split()) == 1
        assert all(1 <= len(q.split()) <= 3 for q in qs)


def test_q2_is_two_words_so_a_renamed_copy_stays_findable():
    qs = se.build_queries(toks(CASES / "C10" / "C10_renamed_of_C1.js"))
    assert len(qs) == 3 and len(qs[1].split()) == 2 and len(qs[2].split()) == 1
    assert "runFetchJob" not in qs[1] and "URLSearchParams" in qs[1]


def test_q2_and_q3_need_two_rare_tokens():
    t = tokenize(["def zzqx_handler():", "    pass"], "hash")
    assert len(se.build_queries(t)) == 1


@pytest.mark.parametrize("token", [
    "sk_" "live_51H8abcdefghijklmnopqrstuvwxyz", "10.0.0.12", "db.corp.internal", "a@b.org",
    "adminPassword", "client_secret", "refreshToken", "AKIAIOSFODNN7EXAMPLE", "fe80::1ff:fe23:4567:890a",
    "eyJhbGciOiJIUzI1NiJ9.e30.abc", "/etc/passwd", "C:\\Users\\x", "intranet.local", "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
])
def test_looks_sensitive_true(token):
    assert se.looks_sensitive(token)


@pytest.mark.parametrize("token", ["URLSearchParams", "jsonplaceholder.typicode.com", "doAsyncTask", "ChainedAssignent",
                                   "dict_of_dicts_merge", "NoamOpt"])
def test_looks_sensitive_false(token):
    assert not se.looks_sensitive(token)


def test_sensitive_tokens_never_queried(tmp_path):
    src = (
        "const prodKey = 'sk_" "live_51H8abcdefghijklmnopqrstuvwxyz';\n"
        "const ip = '10.0.0.12';\n"
        "const host = 'https://db.corp.internal/v1';\n"
        "const mail = 'a@b.org';\n"
        "const adminPassword = loadSecretToken();\n"
        "function computeInvoiceTotals(lineItems) {\n"
        "  const subtotalAmount = lineItems.reduce((a, b) => a + b.price * b.qty, 0);\n"
        "  const taxAmount = subtotalAmount * 0.21;\n"
        "  const shippingFee = subtotalAmount > 100 ? 0 : 9.5;\n"
        "  const discountRate = lineItems.length > 10 ? 0.1 : 0;\n"
        "  const discountAmount = subtotalAmount * discountRate;\n"
        "  return { subtotalAmount, taxAmount, shippingFee, total: subtotalAmount + taxAmount + shippingFee };\n"
        "}\n"
    )
    root = mk(tmp_path / "p", {"src/billing.js": src})
    plan = se.make_plan(root, ["src"])
    text = unquote_plus(planned_text(plan)).lower()
    for bad in ("sk_live", "10.0.0.12", "corp.internal", "a@b.org", "password", "secret", "token"):
        assert bad not in text
    assert plan["requests"], "the safe identifiers should still produce a query"


# ---- plan ---------------------------------------------------------------------------

def test_plan_shape_c1(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    assert plan["backend"] == "stackexchange" and plan["tier"] == "open"
    urls = [r["url"] for r in plan["requests"]]
    assert urls and all(u.startswith(se.API + "/search/excerpts?q=") for u in urls)
    assert all("site=stackoverflow" in u and "key=" not in u for u in urls)
    assert all(r["method"] == "GET" and r["body"] is None for r in plan["requests"])
    assert len({r["id"] for r in plan["requests"]}) == len(urls)
    kinds = {f["url_template"].split("?")[0] for f in plan["followups"]}
    assert kinds == {se.API + "/answers/{ids}", se.API + "/posts/{ids}/revisions"}
    assert all(f["id_param"] == "ids" and f["max_ids"] <= 100 for f in plan["followups"])
    assert any(se.ANSWER_FILTER in f["url_template"] for f in plan["followups"])
    # q1 pages 1 and 2, q2 page 1
    pages = sorted(parse_qs(urlsplit(u).query)["page"][0] for u in urls)
    assert pages.count("1") >= 1 and pages.count("2") == 1


def test_plan_requires_include(tmp_path):
    root = case(tmp_path, "C1")
    with pytest.raises(se.StackExchangeError) as ei:
        se.make_plan(root, [])
    assert ei.value.code == "include_required"


def test_plan_only_selected_files_and_never_sensitive(tmp_path):
    body = Path(CASES / "C1" / "C1_so_2019_cc4.js").read_text(encoding="utf-8")
    root = mk(tmp_path / "p", {
        "src/ok.js": body,
        ".env": "TOKEN=abc\n",
        "src/.env": "TOKEN=abc\n",
        "src/node_modules/x.js": body.replace("doAsyncTask", "vendoredFnName"),
        "src/blob.bin": b"\0\1\2" * 100,
        "src/big.js": ("var xx = 1;\n" * 200_000).encode(),
        "other/not_included.js": body.replace("doAsyncTask", "notIncludedFnName"),
    })
    plan = se.make_plan(root, ["src"])
    text = unquote_plus(planned_text(plan))
    for bad in ("vendoredFnName", "notIncludedFnName", "TOKEN"):
        assert bad not in text
    reasons = {e["reason"] for e in plan["excluded"]}
    assert {"vendored", "too_large"} <= reasons or {"vendored"} <= reasons


def test_plan_capped_and_lists_unplanned_blocks(tmp_path):
    funcs = []
    for i in range(12):
        name = f"uniqueFunctionNumber{chr(97 + i)}Zq"
        funcs.append(f"function {name}(alpha, beta) {{\n"
                     f"  const first{chr(97 + i)}Total = alpha * 2 + beta;\n"
                     f"  const second{chr(97 + i)}Total = first{chr(97 + i)}Total * 3 - alpha;\n"
                     f"  if (second{chr(97 + i)}Total > 100) {{ return second{chr(97 + i)}Total; }}\n"
                     f"  const third{chr(97 + i)}Total = second{chr(97 + i)}Total / 7 + alpha;\n"
                     f"  console.log(third{chr(97 + i)}Total);\n"
                     f"  return first{chr(97 + i)}Total + second{chr(97 + i)}Total;\n"
                     f"}}\n")
    root = mk(tmp_path / "p", {"src/many.js": "\n\n".join(funcs)})
    plan = se.make_plan(root, ["src"], max_requests=6)
    assert len(plan["requests"]) <= 6
    capped = [e for e in plan["excluded"] if e["reason"] == "cap"]
    assert capped and all(re.search(r":\d+-\d+$", e["rel"]) for e in capped)
    assert plan["cost"]["requests"] == len(plan["requests"])


def test_plan_keyless_by_default(tmp_path, monkeypatch):
    root = case(tmp_path, "C1")
    monkeypatch.setenv("STACKEXCHANGE_KEY", "abcSECRETkey123456")
    plan = se.make_plan(root, ["C1"])
    out = json.dumps(plan) + approval.render_preview(plan)
    assert "key=" not in out and "abcSECRETkey123456" not in out
    keyed = se.make_plan(root, ["C1"], use_key=True)
    assert all("key={secret:STACKEXCHANGE_KEY}" in r["url"] for r in keyed["requests"])
    assert all("key={secret:STACKEXCHANGE_KEY}" in f["url_template"] for f in keyed["followups"])
    preview = approval.render_preview(keyed)
    assert "keyed" in preview.lower() and "abcSECRETkey123456" not in preview + json.dumps(keyed)


def test_use_key_without_env_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("STACKEXCHANGE_KEY", raising=False)
    root = case(tmp_path, "C1")
    with pytest.raises(se.StackExchangeError) as ei:
        se.make_plan(root, ["C1"], use_key=True)
    assert ei.value.code == "key_missing"


def test_trivial_blocks_not_planned(tmp_path):
    root = case(tmp_path, "C7")
    plan = se.make_plan(root, ["C7"])
    assert plan["requests"] == [] and plan["followups"] == []
    assert {e["reason"] for e in plan["excluded"]} & {"boilerplate", "too_short", "license_header"}
    res, _ = run_plan(tmp_path, plan, root, FakeSE())
    assert res["matches"] == [] and any("nothing_to_look_up" in n for n in res["notes"])


def test_original_code_is_still_searched_but_finds_nothing(tmp_path):
    root = case(tmp_path, "C8")
    plan = se.make_plan(root, ["C8"])
    assert plan["requests"]


def test_local_only_never_in_requests(tmp_path):
    root = case(tmp_path, "C1", "C10")
    plan = se.make_plan(root, ["C1", "C10"])
    text = planned_text(plan)
    for blocks in plan["local_only"]["blocks"].values():
        for b in blocks:
            assert b["rel"] not in text
    assert str(root) not in json.dumps(plan)


def test_preview_lists_every_request_and_disclosure(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    preview = approval.render_preview(plan)
    for r in plan["requests"]:
        assert r["url"] in preview
    for line in se.DISCLOSURE:
        assert line in preview


# ---- parsing helpers ----------------------------------------------------------------

def test_answer_code_blocks_unescape():
    blocks = se.answer_code_blocks(ANSWERS[58437909]["body"])
    assert any("new URLSearchParams(" in b for b in blocks)
    assert all("<" not in b.split("\n")[0] or "=>" in b or "<" in b for b in blocks)
    html = "<p>x</p><pre><code>if (a &lt; b &amp;&amp; c) { <b>x</b> }</code></pre>"
    assert se.answer_code_blocks(html) == ["if (a < b && c) { x }"]


def ts(y, mo, d, h=0, mi=0, s=0):
    import calendar
    return calendar.timegm((y, mo, d, h, mi, s))


def test_date_rule_boundaries():
    assert se.date_rule_license(ts(2011, 4, 7, 23, 59, 59)) == "CC-BY-SA-2.5"
    assert se.date_rule_license(ts(2011, 4, 8)) == "CC-BY-SA-3.0"
    assert se.date_rule_license(ts(2018, 5, 1, 23, 59, 59)) == "CC-BY-SA-3.0"
    assert se.date_rule_license(ts(2018, 5, 2)) == "CC-BY-SA-4.0"


def test_normalise_content_license():
    assert se.spdx_from_content_license("CC BY-SA 4.0") == "CC-BY-SA-4.0"
    assert se.spdx_from_content_license("CC BY-SA 3.0") == "CC-BY-SA-3.0"
    assert se.spdx_from_content_license("CC BY-SA 2.5") == "CC-BY-SA-2.5"
    assert se.spdx_from_content_license("weird") is None


def user_tokens_c1():
    return toks(CASES / "C1" / "C1_so_2019_cc4.js")


def test_license_by_revision_preferred():
    # SYNTHETIC revision list: revision 1 (2019, 4.0) holds the snippet; revision 2 changed it.
    code = ANSWERS[58437909]["body"]
    revs = [
        {"revision_number": 2, "creation_date": ts(2020, 1, 1), "content_license": "CC BY-SA 4.0",
         "body": "<pre><code>completely different\ncode here\n</code></pre>"},
        {"revision_number": 1, "creation_date": ts(2019, 10, 17), "content_license": "CC BY-SA 3.0",
         "body": code},
    ]
    out = se.resolve_license(ANSWERS[58437909], revs, user_tokens_c1())
    assert out[0]["spdx"] == "CC-BY-SA-3.0" and out[0]["evidence"] == "so_revision"
    assert out[0]["confidence"] == "high" and out[0]["status"] == "declared"
    assert out[0].get("revision") == "1"


def test_license_fallback_lists_current_and_date_rule():
    ans = ANSWERS[26853961]          # created 2014, current licence 4.0
    out = se.resolve_license(ans, None, user_tokens_c1())
    by_ev = {o["evidence"]: o for o in out}
    assert by_ev["so_answer_current"]["spdx"] == "CC-BY-SA-4.0"
    assert by_ev["so_answer_current"]["confidence"] == "medium"
    assert by_ev["so_date_rule"]["spdx"] == "CC-BY-SA-3.0"
    assert by_ev["so_date_rule"]["confidence"] == "low"


def test_license_fallback_same_value_listed_once():
    out = se.resolve_license(ANSWERS[58437909], None, user_tokens_c1())
    assert [o["evidence"] for o in out] == ["so_answer_current"]


# ---- run: replay-style, with a fake service -------------------------------------------

def run_plan(tmp_path, plan, root, opener, sleep=None, **kw):
    sleeps = []
    res = se.run(plan_file(tmp_path, plan), plan["sha256"], root, opener=opener,
                 sleep=sleep or sleeps.append, **kw)
    return res, sleeps


def test_run_replay_c1(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    fake = FakeSE(search=hit_c1)
    res, _ = run_plan(tmp_path, plan, root, fake)
    assert not res["errors"], res["errors"]
    assert res["backend"] == "stackexchange" and res["tier"] == "open"
    assert res["root"] == plan["root"] and res["plan_sha256"] == plan["sha256"]
    ms = res["matches"]
    assert ms and ms[0]["match_type"] in ("exact", "near")
    c = ms[0]["candidate"]
    assert c["origin_url"] == "https://stackoverflow.com/a/58437909"
    assert c["kind"] == "stackoverflow" and c["backend"] == "stackexchange"
    assert c["author"] == "Scotty Jamison" and c["author_url"].startswith("https://stackoverflow.com/users/")
    assert c["post_id"] == 58437909
    assert c["licenses"][0]["spdx"] == "CC-BY-SA-4.0"
    assert ms[0]["rel"] == "C1/C1_so_2019_cc4.js" and 1 <= ms[0]["start"] <= ms[0]["end"]
    assert all(s["status"] == 200 for s in res["requests_sent"])
    sent = {s["url"] for s in res["requests_sent"]}
    planned = {r["url"] for r in plan["requests"]}
    assert sent - planned and all(u.startswith(se.API + "/answers/") or u.startswith(se.API + "/posts/")
                                  for u in sent - planned)


def test_run_c10_finds_renamed_copy_as_normalised(tmp_path):
    root = case(tmp_path, "C10")
    plan = se.make_plan(root, ["C10"])
    res, _ = run_plan(tmp_path, plan, root, FakeSE(search=hit_c1))
    assert res["matches"][0]["match_type"] == "normalised"
    assert res["matches"][0]["candidate"]["origin_url"] == "https://stackoverflow.com/a/58437909"


def test_run_replay_c2_cc3(tmp_path):
    root = case(tmp_path, "C2")
    plan = se.make_plan(root, ["C2"])
    fake = FakeSE(search=lambda q, page: [7220510] if "syntaxHighlight" in q else [])
    res, _ = run_plan(tmp_path, plan, root, fake)
    c = res["matches"][0]["candidate"]
    assert c["origin_url"] == "https://stackoverflow.com/a/7220510"
    assert c["licenses"][0]["spdx"] == "CC-BY-SA-3.0"


def test_run_c1b_licence_lists_current_and_date_rule(tmp_path):
    root = case(tmp_path, "C1b")
    plan = se.make_plan(root, ["C1b"])
    fake = FakeSE(search=lambda q, page: [26853961])
    res, _ = run_plan(tmp_path, plan, root, fake)
    c = res["matches"][0]["candidate"]
    assert {l["evidence"] for l in c["licenses"]} == {"so_answer_current", "so_date_rule"}


def test_empty_items_no_match_no_error(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    res, _ = run_plan(tmp_path, plan, root, FakeSE())
    assert res["matches"] == [] and res["errors"] == [] and res["not_sent"] == []
    assert any("no_match" in n or "searched" in n for n in res["notes"])


def test_quota_zero_stops_and_lists_unsent(tmp_path):
    root = case(tmp_path, "C1", "C2")
    plan = se.make_plan(root, ["C1", "C2"])
    assert len(plan["requests"]) >= 3
    fake = FakeSE(search=hit_c1, quota=0)
    res, _ = run_plan(tmp_path, plan, root, fake)
    assert len(res["requests_sent"]) < len(plan["requests"]) + 2
    assert res["not_sent"] and all(n["reason"] == "quota_exhausted" for n in res["not_sent"]
                                   if n["id"].startswith("se-"))
    assert res["errors"][0]["code"] == "quota_exhausted"
    assert len([u for u in fake.urls if "/search/excerpts" in u]) == 1


def test_backoff_honoured(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    fake = FakeSE(search=lambda q, p: [], backoff=5, has_more=True)
    res, sleeps = run_plan(tmp_path, plan, root, fake)
    assert 5 in sleeps
    assert len(fake.urls) == len(plan["requests"])      # still sent, just later


def test_backoff_is_bounded(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    fake = FakeSE(search=lambda q, p: [], backoff=100000, has_more=True)
    res, sleeps = run_plan(tmp_path, plan, root, fake)
    assert max(sleeps) <= se.MAX_BACKOFF


def test_throttle_error_502_degrades(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    fake = FakeSE(search_status=400, search_body={"error_id": 502, "error_name": "throttle_violation",
                                                    "error_message": "too many requests"})
    res, _ = run_plan(tmp_path, plan, root, fake)
    assert res["errors"][0]["code"] == "quota_exhausted"
    assert res["matches"] == []
    assert {n["id"] for n in res["not_sent"]} >= {r["id"] for r in plan["requests"]}


def test_http_429_is_quota_exhausted(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    fake = FakeSE(search_status=429, search_body={})
    res, _ = run_plan(tmp_path, plan, root, fake)
    assert res["errors"][0]["code"] == "quota_exhausted"


def test_network_error_is_coverage_error(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])

    def boom(req, timeout=None):
        raise urllib.error.URLError("down")

    res, _ = run_plan(tmp_path, plan, root, boom)
    assert res["errors"] and res["errors"][0]["code"] == "network_error"
    assert {n["id"] for n in res["not_sent"]} == {r["id"] for r in plan["requests"]}


def test_empty_body_is_bad_response(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])

    def empty(req, timeout=None):
        return Resp(200, b"")

    res, _ = run_plan(tmp_path, plan, root, empty)
    assert any(e["code"] == "bad_response" for e in res["errors"])


def test_hash_mismatch_sends_nothing(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    p = plan_file(tmp_path, plan)

    def never(req, timeout=None):
        raise AssertionError("must not be called")

    with pytest.raises(approval.ApprovalError) as ei:
        se.run(p, "0" * 64, root, opener=never)
    assert ei.value.code == "plan_hash_mismatch"


def test_file_changed_since_plan_skipped(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    f = root / "C1" / "C1_so_2019_cc4.js"
    f.write_text(f.read_text(encoding="utf-8") + "\nconsole.log('changed');\nlet y = 2;\n", encoding="utf-8")
    f.write_text(f.read_text(encoding="utf-8").replace("doAsyncTask", "doOtherTask"), encoding="utf-8")
    fake = FakeSE(search=hit_c1)
    res, _ = run_plan(tmp_path, plan, root, fake)
    assert fake.urls == []
    assert res["matches"] == []
    assert any("file_changed_since_plan" in n for n in res["notes"])
    assert {n["id"] for n in res["not_sent"]} == {r["id"] for r in plan["requests"]}
    assert all(n["reason"] == "file_changed_since_plan" for n in res["not_sent"])


def test_root_name_mismatch_is_an_error(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    other = tmp_path / "elsewhere"
    shutil.copytree(root, other)
    with pytest.raises(se.StackExchangeError) as ei:
        se.run(plan_file(tmp_path, plan), plan["sha256"], other, opener=FakeSE())
    assert ei.value.code == "root_mismatch"


def test_followup_ids_only_from_search_results(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    guard = approval.Guard(plan)
    guard.allow_ids([58437909])
    tpl = next(f for f in plan["followups"] if "/answers/" in f["url_template"])["url_template"]
    guard.check("GET", tpl.replace("{ids}", "58437909"), None)
    with pytest.raises(approval.ApprovalError) as ei:
        guard.check("GET", tpl.replace("{ids}", "123456"), None)
    assert ei.value.code == "followup_not_allowed"


def test_run_sends_only_planned_and_allowed_urls(tmp_path):
    root = case(tmp_path, "C1", "C2")
    plan = se.make_plan(root, ["C1", "C2"])
    fake = FakeSE(search=lambda q, p: [58437909, 7220510])
    res, _ = run_plan(tmp_path, plan, root, fake)
    allowed_followup = re.compile(r"^" + re.escape(se.API) + r"/(answers|posts)/(\d+;?)+(/revisions)?\?")
    planned = {r["url"] for r in plan["requests"]}
    for u in fake.urls:
        assert u in planned or allowed_followup.match(u), u
    ids = {int(x) for u in fake.urls if allowed_followup.match(u) for x in re.findall(r"\d+", urlsplit(u).path.split("/", 3)[3])}
    assert ids <= {58437909, 7220510}


def test_narrow_results_win_the_answer_cap(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    junk = list(range(1000, 1060))                       # 60 unrelated answer ids from the broad query

    def search(q, page):
        return [58437909] if len(q.split()) > 1 else junk[(page - 1) * 30:page * 30]

    fake = FakeSE(search=search, has_more=True)
    res, _ = run_plan(tmp_path, plan, root, fake)
    assert res["matches"] and res["matches"][0]["candidate"]["post_id"] == 58437909
    fetched = [u for u in fake.urls if "/answers/" in u][0]
    assert len(fetched.split("/answers/")[1].split("?")[0].split(";")) <= se.SE_MAX_ANSWERS_PER_BLOCK


def test_page_two_skipped_when_page_one_complete(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    fake = FakeSE(search=hit_c1, has_more=False)
    res, _ = run_plan(tmp_path, plan, root, fake)
    pages = [parse_qs(urlsplit(u).query)["page"][0] for u in fake.urls if "/search/excerpts" in u]
    assert "2" not in pages
    assert res["not_sent"] == []
    assert any("page 2" in n or "page_2" in n for n in res["notes"])


def test_weak_candidate_below_threshold_not_matched(tmp_path):
    root = case(tmp_path, "C8")
    plan = se.make_plan(root, ["C8"])
    fake = FakeSE(search=lambda q, p: [58437909])
    res, _ = run_plan(tmp_path, plan, root, fake)
    assert res["matches"] == []


def test_results_roundtrip_and_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("STACKEXCHANGE_KEY", "abcSECRETkey123456")
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"], use_key=True)
    res, _ = run_plan(tmp_path, plan, root, FakeSE(search=hit_c1))
    assert "abcSECRETkey123456" not in json.dumps(res)
    assert all("abcSECRETkey123456" not in u for u in FakeSE().urls)


def test_cli_plan_and_run(tmp_path, capsys):
    root = case(tmp_path, "C1")
    out = tmp_path / "plan.json"
    assert se.main(["plan", str(root), "--include", "C1", "--out", str(out)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["sha256"] and "preview" in data and Path(data["plan_path"]).exists()
    res_path = tmp_path / "res.json"
    rc = se.main(["run", str(out), "--sha256", "f" * 64, "--root", str(root), "--out", str(res_path)])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["error"] == "plan_hash_mismatch"


def test_cli_missing_include_is_an_error(tmp_path, capsys):
    root = case(tmp_path, "C1")
    rc = se.main(["plan", str(root), "--out", str(tmp_path / "p.json")])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["error"] in ("include_required", "usage")


# ---- recorded live responses (2026-10-24, public fixture identifiers only) -------------------------

def recorded_run(tmp_path):
    """Replays the live capture: the plan the evaluation harness builds over all fixture cases (38 searches)."""
    import evaluate as ev
    from replay import ReplayOpener
    cases = ev.load_cases(FIXTURES / "cases.json")
    work = ev._Work(cases, FIXTURES, tmp_path / "work")
    includes = [i for c in cases for i in ev._case_includes(c, c["id"] + "/")]
    plan = se.make_plan(work.combined, includes, max_requests=ev.OPEN_MAX_REQUESTS)
    opener = ReplayOpener(RESPONSES)
    res, _ = run_plan(tmp_path, plan, work.combined, opener)
    return plan, res, opener


def test_recorded_run_matches_live_capture(tmp_path):
    plan, res, opener = recorded_run(tmp_path)
    assert res["errors"] == [] and res["not_sent"] == []
    found = {}
    for m in res["matches"]:
        found.setdefault(m["rel"].split("/")[0], []).append((m["candidate"]["post_id"], m["match_type"],
                                                             m["candidate"]["licenses"][0]))
    assert (58437909, "exact") == found["C1"][0][:2]
    assert (58437909, "normalised") == found["C10"][0][:2]          # renamed copy found through q2
    assert (53954986, "exact") == found["C1p"][0][:2]
    assert 26853961 in {f[0] for f in found["C1b"]}
    assert 66212699 in {f[0] for f in found["C9"]}                   # the Annotated Transformer origin of C9
    for key in ("C5", "C8"):
        assert key not in found                                      # no false positive
    # honest miss: the open tier did not find the C2 answer (7220510) within 60 results of its only rare word
    assert "C2" not in found
    for lic in (f[2] for fs in found.values() for f in fs):
        assert lic["evidence"] == "so_revision" and lic["confidence"] == "high"


def test_recorded_run_stays_inside_plan(tmp_path):
    plan, res, opener = recorded_run(tmp_path)
    planned = {r["url"] for r in plan["requests"]}
    for s in res["requests_sent"]:
        assert s["url"] in planned or s["followup"]
    assert all(s["status"] == 200 for s in res["requests_sent"])


def test_recorded_revisions_carry_bodies():
    from replay import ReplayOpener
    idx = json.loads((RESPONSES / "index.json").read_text(encoding="utf-8"))
    revs = [v for k, v in idx.items() if "/revisions" in k]
    assert revs
    items = [it for v in revs for it in json.loads((RESPONSES / v["file"]).read_text(encoding="utf-8"))["items"]]
    assert any(isinstance(it.get("body"), str) and it.get("content_license") for it in items)


def test_revisions_has_more_falls_back_to_current_licence_at_low_confidence(tmp_path):
    root = case(tmp_path, "C1")
    plan = se.make_plan(root, ["C1"])
    idx = json.loads((RESPONSES / "index.json").read_text(encoding="utf-8"))
    revs = {}
    for k, v in idx.items():
        if "/revisions" in k:
            for it in json.loads((RESPONSES / v["file"]).read_text(encoding="utf-8"))["items"]:
                revs.setdefault(it["post_id"], []).append(it)

    class Paged(FakeSE):
        def wrap(self, items, **extra):
            return super().wrap(items, has_more=bool(items) and "post_id" in items[0], **extra)

    # has_more on the revisions follow-up: the list may lack the earliest revision, so it is not trusted
    res, _ = run_plan(tmp_path, plan, root, Paged(search=hit_c1, revisions=revs))
    assert res["matches"] and "post_id" not in str(res["errors"])
    assert all(m["candidate"]["licenses"][0]["evidence"] == "so_answer_current"
               and m["candidate"]["licenses"][0]["confidence"] == "low"
               and "incomplete" in m["candidate"]["note"] for m in res["matches"])
