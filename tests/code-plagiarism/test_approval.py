"""Plan/run approval gate: the privacy boundary of the skill."""
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import approval  # noqa: E402
from approval import ApprovalError, Guard  # noqa: E402

BASE = "https://api.stackexchange.com/2.3"
R1 = {"id": "r1", "method": "GET", "url": BASE + "/search/excerpts?q=alpha+beta&site=stackoverflow",
      "body": None, "purpose": "find alpha", "source": {"rel": "src/a.js", "start": 3, "end": 14}}
R2 = {"id": "r2", "method": "POST", "url": "https://api.osskb.org/scan/direct",
      "body": "file=abc,0,1.js\nline=1\n", "purpose": "fingerprint", "source": None}
R3 = {"id": "r3", "method": "GET", "url": BASE + "/search/excerpts?q=gamma&site=stackoverflow",
      "body": None, "purpose": "find gamma", "source": {"rel": "src/b.js", "start": 1, "end": 9}}
FU = {"method": "GET", "url_template": BASE + "/answers/{ids}?site=stackoverflow&filter=x",
      "id_param": "ids", "max_ids": 3, "max_requests": 10}


def make(requests=None, followups=None, **kw):
    args = dict(backend="stackexchange", tier="open", root="C:/work/myrepo",
                requests=copy.deepcopy(requests if requests is not None else [R1, R2, R3]),
                followups=copy.deepcopy(followups if followups is not None else [FU]),
                disclosure=["identifiers leave the machine"], cost={"requests": 3, "quota": "3 of 300/day"},
                excluded=[{"rel": ".env", "reason": "sensitive"}],
                local_only={"map": {"1.js": "src/a.js"}})
    args.update(kw)
    return approval.build_plan(**args)


def written(tmp_path, plan=None, name="plan.json"):
    plan = plan or make()
    p = tmp_path / name
    approval.write_plan(plan, p)
    return p, plan


def code(excinfo):
    return excinfo.value.code


# ---- hash / load ---------------------------------------------------------

def test_plan_hash_canonical():
    plan = make()
    shuffled = json.loads(json.dumps(plan))
    shuffled = {k: shuffled[k] for k in reversed(list(shuffled))}
    shuffled["requests"][0] = {k: shuffled["requests"][0][k] for k in reversed(list(shuffled["requests"][0]))}
    assert approval.plan_hash(shuffled) == plan["sha256"]
    assert plan["version"] == approval.PLAN_VERSION == 1
    assert plan["created"].endswith("Z")


def test_plan_hash_covers_everything():
    plan = make()
    base = plan["sha256"]
    for mutate in (
        lambda p: p["requests"][0].update(body="x"),
        lambda p: p["requests"][0].update(purpose="other"),
        lambda p: p["requests"][0].update(headers={"Accept": "application/json"}),
        lambda p: p["followups"][0].update(max_ids=2),
        lambda p: p["local_only"].update(extra="y"),
        lambda p: p.update(tier="strict"),
        lambda p: p.update(backend="other"),
        lambda p: p["disclosure"].append("z"),
        lambda p: p["excluded"].append({"rel": "k", "reason": "r"}),
        lambda p: p["cost"].update(requests=99),
    ):
        q = copy.deepcopy(plan)
        mutate(q)
        assert approval.plan_hash(q) != base


def test_load_plan_roundtrip(tmp_path):
    p, plan = written(tmp_path)
    assert approval.load_plan(p, plan["sha256"]) == plan


def test_load_plan_missing(tmp_path):
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(tmp_path / "nope.json", "0" * 64)
    assert code(e) == "plan_not_found"


def test_load_plan_rejects_edited_file(tmp_path):
    p, plan = written(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace("find alpha", "find alphX"), encoding="utf-8")
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(p, plan["sha256"])
    assert code(e) == "plan_hash_mismatch"
    p, plan = written(tmp_path, name="p2.json")
    p.write_text(p.read_text(encoding="utf-8").replace("search/excerpts?q=alpha", "search/excerpts?q=evil"),
                 encoding="utf-8")
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(p, plan["sha256"])
    assert code(e) == "plan_hash_mismatch"


def test_load_plan_rejects_whitespace_only_edit(tmp_path):
    p, plan = written(tmp_path)
    p.write_bytes(p.read_bytes() + b"\n")
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(p, plan["sha256"])
    assert code(e) == "plan_hash_mismatch"
    p, plan = written(tmp_path, name="p2.json")
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(p, plan["sha256"])
    assert code(e) == "plan_hash_mismatch"


def test_load_plan_rejects_wrong_approved_hash(tmp_path):
    p, plan = written(tmp_path)
    for bad in ("0" * 64, "", None, plan["sha256"][:-1], plan["sha256"] + "0"):
        with pytest.raises(ApprovalError) as e:
            approval.load_plan(p, bad)
        assert code(e) == "plan_hash_mismatch"


def _rewrite_valid_hash(tmp_path, mutate, name="m.json"):
    """Edit the plan AND recompute a consistent hash (an attacker who also fixes the hash)."""
    plan = make()
    mutate(plan)
    plan["sha256"] = approval.plan_hash(plan)
    p = tmp_path / name
    approval.write_plan(plan, p)
    return p, plan["sha256"]


@pytest.mark.parametrize("mutate", [
    lambda p: p["requests"].append(dict(p["requests"][0], url=BASE + "/other")),          # duplicate id
    lambda p: p["requests"][0].update(extra_field="x"),                                    # unknown field
    lambda p: p["followups"][0].update(redirect="follow"),
    lambda p: p.update(version=2),
    lambda p: p.update(unknown_top="x"),
    lambda p: p["requests"][0].update(method="DELETE"),
    lambda p: p["requests"][0].update(url="http://api.stackexchange.com/x"),
    lambda p: p["requests"][0].update(url="https://user@evil.example/x"),
    lambda p: p["requests"][0].update(url=BASE + "/x#frag"),
    lambda p: p["requests"][0].update(url=BASE + "/x\r\nHost: evil"),
    lambda p: p["requests"][0].update(url=BASE + "/a b"),
    lambda p: p["requests"][1].update(url=R1["url"], body=None, method="GET"),             # same request twice
    lambda p: p["requests"][0].update(headers={"Authorization": "Bearer abc"}),            # literal secret
    lambda p: p["requests"][0].update(headers={"X-Api-Key": "abc"}),
    lambda p: p["requests"][0].update(headers={"User-Agent": "x"}),
    lambda p: p["requests"][0].update(headers={"X-A\r\nB": "v"}),
    lambda p: p["requests"][0].update(headers={"Accept": "a\r\nCookie: z"}),
    lambda p: p["requests"][0].update(headers={"X-Key": {"secret_from_env": "bad name"}}),
    lambda p: p["followups"][0].update(url_template=BASE + "/answers/x"),                  # no {ids}
    lambda p: p["followups"][0].update(max_ids=0),
    lambda p: p["followups"][0].update(max_ids=True),
    lambda p: p["followups"][0].update(id_param="other"),
    lambda p: p.update(tier="nuclear"),
    lambda p: p["local_only"].update(map={"1.js": "C:\\Users\\me\\src\\a.js"}),
    lambda p: p["local_only"].update(map={"1.js": "/home/me/src/a.js"}),
    lambda p: p["requests"][0].update(source={"rel": "/etc/passwd", "start": 1, "end": 2}),
    lambda p: p.update(created="yesterday"),
])
def test_load_plan_rejects_invalid_even_with_valid_hash(tmp_path, mutate):
    p, h = _rewrite_valid_hash(tmp_path, mutate)
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(p, h)
    assert code(e) in ("plan_invalid", "absolute_path_in_plan")


def test_load_plan_rejects_duplicate_json_keys(tmp_path):
    p, plan = written(tmp_path)
    text = p.read_text(encoding="utf-8")
    assert '"tier": "open",' in text
    p.write_text(text.replace('"tier": "open",', '"tier": "strict",\n  "tier": "open",'), encoding="utf-8")
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(p, plan["sha256"])
    assert code(e) in ("plan_invalid", "plan_hash_mismatch")


def test_load_plan_rejects_future_created(tmp_path):
    fut = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p, h = _rewrite_valid_hash(tmp_path, lambda pl: pl.update(created=fut))
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(p, h)
    assert code(e) == "plan_invalid"
    near = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p, h = _rewrite_valid_hash(tmp_path, lambda pl: pl.update(created=near), name="n.json")
    assert approval.load_plan(p, h)["created"] == near


def test_load_plan_not_json(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("not json", encoding="utf-8")
    with pytest.raises(ApprovalError) as e:
        approval.load_plan(p, "0" * 64)
    assert code(e) == "plan_invalid"


def test_build_plan_rejects_absolute_local_paths():
    with pytest.raises(ApprovalError) as e:
        make(local_only={"map": {"1.js": "C:/Users/me/a.js"}})
    assert code(e) == "absolute_path_in_plan"


def test_plan_has_no_absolute_root(tmp_path):
    plan = make(root=str(tmp_path / "secretproject"))
    text = json.dumps(plan)
    assert str(tmp_path).replace("\\", "\\\\") not in text and str(tmp_path) not in text
    assert plan["root"] == "secretproject"


# ---- guard -----------------------------------------------------------------

def test_guard_allows_exact_planned_requests():
    g = Guard(make())
    g.check("GET", R1["url"], None)
    g.check("POST", R2["url"], R2["body"].encode("utf-8"))
    assert g.sent == []   # entries are written by net.send once the outcome is known


def test_guard_blocks_unplanned_url_and_changed_body():
    g = Guard(make())
    with pytest.raises(ApprovalError) as e:
        g.check("GET", BASE + "/search/excerpts?q=other", None)
    assert code(e) == "request_not_in_plan"
    with pytest.raises(ApprovalError) as e:
        g.check("POST", R2["url"], b"file=zzz")
    assert code(e) == "request_not_in_plan"
    with pytest.raises(ApprovalError) as e:
        g.check("POST", R2["url"], None)
    assert code(e) == "request_not_in_plan"
    with pytest.raises(ApprovalError) as e:
        g.check("GET", R1["url"], b"smuggled")
    assert code(e) == "request_not_in_plan"
    assert g.sent == []


def _variants(url):
    host = "api.stackexchange.com"
    return {
        "upper host": url.replace(host, host.upper()),
        "upper path": url.replace("/search", "/Search"),
        "trailing slash": url + "/",
        "extra param": url + "&extra=1",
        "reordered params": BASE + "/search/excerpts?site=stackoverflow&q=alpha+beta",
        "userinfo": url.replace("//" + host, "//" + host + "@evil.example"),
        "userinfo2": url.replace("//" + host, "//user:pw@" + host),
        "http scheme": url.replace("https://", "http://"),
        "port": url.replace(host, host + ":8443"),
        "default port": url.replace(host, host + ":443"),
        "pct-encoded": url.replace("alpha", "%61lpha"),
        "plus vs %20": url.replace("alpha+beta", "alpha%20beta"),
        "dotdot": url.replace("/search/", "/x/../search/"),
        "dot": url.replace("/search/", "/./search/"),
        "fragment": url + "#frag",
        "crlf": url + "\r\nX-Evil: 1",
        "lf": url + "\nHost: evil",
        "nul": url + "\x00",
        "space": url + " ",
        "leading space": " " + url,
        "backslash": url.replace("/search", "\\search"),
        "double slash": url.replace("/2.3/", "//2.3/"),
        "unicode host": url.replace("api.", "ap\u0131."),
        "empty query": url.replace("?q=alpha+beta&site=stackoverflow", "?"),
        "evil host": url.replace(host, "evil.example"),
        "subdomain": url.replace("//api", "//evil.api"),
    }


@pytest.mark.parametrize("name", list(_variants(R1["url"])))
def test_guard_rejects_url_variants(name):
    g = Guard(make())
    bad = _variants(R1["url"])[name]
    assert bad != R1["url"]
    with pytest.raises(ApprovalError) as e:
        g.check("GET", bad, None)
    assert code(e) == "request_not_in_plan"
    assert g.sent == []


def test_guard_rejects_method_and_types():
    g = Guard(make())
    for m in ("get", "POST", "Get", "HEAD", "", None, b"GET"):
        with pytest.raises(ApprovalError) as e:
            g.check(m, R1["url"], None)
        assert code(e) == "request_not_in_plan"
    with pytest.raises(ApprovalError):
        g.check("GET", R1["url"].encode(), None)
    with pytest.raises(ApprovalError):
        g.check("POST", R2["url"], R2["body"])  # str body, not bytes
    with pytest.raises(ApprovalError):
        g.check("POST", R2["url"], bytearray(R2["body"].encode()))


def test_guard_body_must_be_byte_identical():
    g = Guard(make())
    for body in (R2["body"].replace("\n", "\r\n"), R2["body"] + " ", R2["body"].upper()):
        with pytest.raises(ApprovalError):
            g.check("POST", R2["url"], body.encode())
    plan = make(requests=[dict(R2, id="u", body="caf\u00e9")], followups=[])
    g2 = Guard(plan)
    g2.check("POST", R2["url"], "caf\u00e9".encode("utf-8"))
    with pytest.raises(ApprovalError):
        g2.check("POST", R2["url"], "caf\u00e9".encode("latin-1"))
    with pytest.raises(ApprovalError):
        g2.check("POST", R2["url"], "cafe\u0301".encode("utf-8"))


def test_followup_only_with_returned_ids():
    g = Guard(make())
    base = BASE + "/answers/{}?site=stackoverflow&filter=x"
    with pytest.raises(ApprovalError) as e:
        g.check("GET", base.format("1;2"), None)
    assert code(e) == "followup_not_allowed"          # nothing allowed yet
    g.allow_ids({2, 1})
    g.check("GET", base.format("1;2"), None)
    g.check("GET", base.format("2"), None)
    for bad, c in [(base.format("1;3"), "followup_not_allowed"),
                   (base.format("3"), "followup_not_allowed"),
                   (base.format("2;1"), "followup_not_allowed"),       # not ascending
                   (base.format("1;1"), "followup_not_allowed"),       # duplicates
                   (base.format("01"), "followup_not_allowed"),
                   (base.format("1;"), "followup_not_allowed"),
                   (base.format(""), "followup_not_allowed"),
                   (base.format("1,2"), "followup_not_allowed"),
                   (base.format("1%3B2"), "followup_not_allowed"),
                   (base.format("1;2") + "&x=1", "request_not_in_plan"),
                   (BASE + "/comments/1?site=stackoverflow&filter=x", "request_not_in_plan"),
                   ("https://evil.example/answers/1?site=stackoverflow&filter=x", "request_not_in_plan")]:
        with pytest.raises(ApprovalError) as e:
            g.check("GET", bad, None)
        assert code(e) == c, bad
    with pytest.raises(ApprovalError) as e:
        g.check("POST", base.format("1"), None)
    assert code(e) == "request_not_in_plan"
    with pytest.raises(ApprovalError) as e:
        g.check("GET", base.format("1"), b"body")
    assert code(e) == "request_not_in_plan"


def test_followup_max_ids_and_allow_ids_validation():
    g = Guard(make())
    g.allow_ids([1, 2, 3, 4])
    base = BASE + "/answers/{}?site=stackoverflow&filter=x"
    g.check("GET", base.format("1;2;3"), None)
    with pytest.raises(ApprovalError) as e:
        g.check("GET", base.format("1;2;3;4"), None)
    assert code(e) == "followup_not_allowed"
    for bad in ("1;2", -1, 0, 2 ** 63, 1.5, True, None):
        with pytest.raises(ApprovalError) as e:
            g.allow_ids([bad])
        assert code(e) == "followup_not_allowed"


def test_guard_without_followups_rejects_answers():
    g = Guard(make(followups=[]))
    g.allow_ids([1])
    with pytest.raises(ApprovalError) as e:
        g.check("GET", BASE + "/answers/1?site=stackoverflow&filter=x", None)
    assert code(e) == "request_not_in_plan"


def test_guard_rejects_tampered_in_memory_plan():
    plan = make()
    plan["requests"][0]["url"] = "https://evil.example/"
    with pytest.raises(ApprovalError) as e:
        Guard(plan)
    assert code(e) == "plan_hash_mismatch"


def test_guard_headers_must_be_planned():
    reqs = [dict(R1, headers={"Accept": "application/json", "X-Key": {"secret_from_env": "STACKEXCHANGE_KEY"}}), R3]
    plan = make(requests=reqs, followups=[])
    Guard(plan).check("GET", R1["url"], None, headers={"Accept": "application/json"})
    Guard(plan).check("GET", R1["url"], None, headers={"accept": "application/json"})
    Guard(plan).check("GET", R1["url"], None)
    g = Guard(plan)
    for bad in ({"Authorization": "Bearer x"}, {"Cookie": "a=b"}, {"Accept": "text/html"},
                {"X-Key": "hunter2"},                      # secrets never come from the caller
                {"Accept": "application/json\r\nCookie: x"},
                {"Accept\r\nCookie": "x"}, {"Accept ": "application/json"},
                {"Accept": "application/json", "ACCEPT": "application/json"}):
        with pytest.raises(ApprovalError) as e:
            g.check("GET", R1["url"], None, headers=bad)
        assert code(e) == "request_not_in_plan"
    g = Guard(plan)
    with pytest.raises(ApprovalError):
        g.check("GET", R3["url"], None, headers={"Accept": "application/json"})  # R3 plans no headers


def test_secret_value_never_in_plan_preview_log_or_errors(tmp_path, monkeypatch):
    secret = "s3cr3t-VALUE-9f8e7d"
    monkeypatch.setenv("STACKEXCHANGE_KEY", secret)
    reqs = [dict(R1, headers={"X-Key": {"secret_from_env": "STACKEXCHANGE_KEY"}})]
    plan = make(requests=reqs, followups=[])
    p, _ = written(tmp_path, plan)
    assert secret not in p.read_text(encoding="utf-8")
    assert secret not in approval.render_preview(plan)
    assert "X-Key" in approval.render_preview(plan) and "STACKEXCHANGE_KEY" in approval.render_preview(plan)
    g = Guard(plan)
    g.check("GET", R1["url"], None)
    g.record("GET", R1["url"], None, status=200, attempts=1)
    assert g.sent and secret not in json.dumps(g.sent)
    with pytest.raises(ApprovalError) as e:
        g.check("GET", R1["url"], None, headers={"X-Key": secret})
    assert secret not in str(e.value)
    assert secret not in repr(e.value)


def test_guard_sent_log_hashes_not_bodies():
    big = "x = 1  # " + "y" * 400
    plan = make(requests=[dict(R2, id="big", body=big)], followups=[])
    g = Guard(plan)
    g.check("POST", R2["url"], big.encode())
    g.record("POST", R2["url"], big.encode(), status=200, attempts=1)
    entry = g.sent[0]
    assert set(entry) >= {"method", "url", "body_sha256"}
    assert entry["body_sha256"] == hashlib.sha256(big.encode()).hexdigest()
    assert big not in json.dumps(g.sent) and "yyyyyyyyyy" not in json.dumps(g.sent)


# ---- strike / preview / local_only -----------------------------------------

def test_strike_removes_request_and_rehashes():
    plan = make()
    new = approval.strike(plan, {"r3"})
    assert [r["id"] for r in new["requests"]] == ["r1", "r2"]
    assert new["sha256"] != plan["sha256"] and new["sha256"] == approval.plan_hash(new)
    assert [r["id"] for r in plan["requests"]] == ["r1", "r2", "r3"]   # input untouched
    with pytest.raises(ApprovalError) as e:
        Guard(new).check("GET", R3["url"], None)
    assert code(e) == "request_not_in_plan"
    with pytest.raises(ApprovalError) as e:
        approval.strike(plan, {"nope"})
    assert code(e) == "plan_invalid"


def test_preview_contains_every_request_verbatim():
    plan = make()
    text = approval.render_preview(plan)
    for r in plan["requests"]:
        assert r["url"] in text and r["method"] in text and r["purpose"] in text
        if r["body"]:
            assert r["body"] in text
    assert plan["sha256"] in text and "open" in text and "api.stackexchange.com" in text
    assert "identifiers leave the machine" in text
    assert "3 of 300/day" in text
    assert ".env" in text and "sensitive" in text
    assert "/answers/{ids}" in text
    assert "src/a.js" in text


def test_local_only_never_in_requests():
    lo = {"map": {"1.js": "src/secret_module.js", "2.js": "src/internal/payroll.py"},
          "hashes": ["deadbeefcafe0123"]}
    plan = make(local_only=lo)
    blob = json.dumps(plan["requests"] + plan["followups"])
    for needle in ("secret_module", "payroll", "deadbeefcafe0123"):
        assert needle not in blob
    assert "secret_module" not in approval.render_preview(plan)
    assert "payroll" not in approval.render_preview(plan)
    assert plan["local_only"] == lo


def test_build_plan_rejects_local_only_leak_into_request():
    lo = {"map": {"1.js": "src/secret_module.js"}}
    with pytest.raises(ApprovalError) as e:
        make(local_only=lo, requests=[dict(R1, url=BASE + "/q?x=src/secret_module.js")])
    assert code(e) == "plan_invalid"


# ---- no bypass -------------------------------------------------------------

def test_no_bypass_flags():
    pat = re.compile(r"bypass|skip[-_ ]?approval|no[-_]?approv|--force|--yes\b|--no-verify|--trust|"
                     r"--insecure|unsafe|(?-i:\bAPPROVE\b)|YOLO|DISABLE_GUARD", re.I)
    for f in SCRIPTS.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        if f.name in ("approval.py", "net.py", "replay.py"):
            # the only environment read is for secret headers named in the plan
            reads = len(re.findall(r"os\.environ|getenv", text))
            assert reads == (1 if f.name == "net.py" else 0), f.name
        for m in pat.finditer(text):
            line = text[:m.start()].count("\n") + 1
            pytest.fail(f"{f.name}:{line}: suspicious token {m.group(0)!r}")


def test_cli_has_only_preview_and_strike(tmp_path):
    p, plan = written(tmp_path)
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "APPROVE": "1"}
    out = subprocess.run([sys.executable, "-I", str(SCRIPTS / "approval.py"), "preview", str(p)],
                         capture_output=True, text=True, encoding="utf-8", env=env)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["sha256"] == plan["sha256"] and R1["url"] in data["preview"]
    assert Path(data["plan_path"]).name == "plan.json"
    for args in (["run", str(p)], ["preview", str(p), "--force"], ["preview", str(p), "--yes"]):
        bad = subprocess.run([sys.executable, "-I", str(SCRIPTS / "approval.py"), *args],
                             capture_output=True, text=True, encoding="utf-8")
        assert bad.returncode == 2


def test_cli_strike(tmp_path):
    p, plan = written(tmp_path)
    out_path = tmp_path / "new.json"
    out = subprocess.run([sys.executable, "-I", str(SCRIPTS / "approval.py"), "strike", str(p),
                          "--ids", "r3,r1", "--out", str(out_path)],
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    loaded = approval.load_plan(out_path, data["sha256"])
    assert [r["id"] for r in loaded["requests"]] == ["r2"]
    assert R1["url"] not in data["preview"] and R3["url"] not in data["preview"]


def test_cli_preview_detects_tampering(tmp_path):
    p, plan = written(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace("alpha", "alphX"), encoding="utf-8")
    out = subprocess.run([sys.executable, "-I", str(SCRIPTS / "approval.py"), "preview", str(p)],
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 2 and json.loads(out.stdout)["error"] == "plan_hash_mismatch"
