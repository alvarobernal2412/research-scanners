"""SCANOSS backend, review fix round 1 (CLI hijack, upload timing/reporting, strict parsing)."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import approval  # noqa: E402
import scanoss_backend as sb  # noqa: E402
from test_scanoss_backend import FakeRunner, js, mk, plan_for, project, run_setup  # noqa: E402


def _exe_names():
    return ["scanoss-py.exe", "scanoss-py.bat", "scanoss-py.cmd"] if os.name == "nt" else ["scanoss-py"]


def _fake_cli(directory, names=None):
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for n in names or _exe_names():
        f = directory / n
        f.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(f, 0o755)
        out.append(f)
    return out


@pytest.fixture
def clean_cli_env(monkeypatch):
    monkeypatch.delenv("SCANOSS_PY", raising=False)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")


def noop_scan(argv, env, cwd):
    Path(argv[argv.index("-o") + 1]).write_text("{}", encoding="utf-8")
    return 0, "", ""


# ---- I-1: the CLI is never taken from the cwd or the scan root ----------------------------------

def test_find_cli_ignores_cwd_and_scan_root(tmp_path, monkeypatch, clean_cli_env):
    cwd, root, empty = tmp_path / "cwd", tmp_path / "root", tmp_path / "empty"
    _fake_cli(cwd)
    _fake_cli(root)
    _fake_cli(root / "sub")
    empty.mkdir()
    monkeypatch.chdir(cwd)
    for path in (str(empty), os.pathsep.join(["", ".", str(cwd), str(root), str(root / "sub"), "bin", str(empty)])):
        monkeypatch.setenv("PATH", path)
        assert sb.find_cli(root) is None


def test_find_cli_prefers_real_path_entry(tmp_path, monkeypatch, clean_cli_env):
    cwd, root, real = tmp_path / "cwd", tmp_path / "root", tmp_path / "real"
    _fake_cli(cwd)
    _fake_cli(root)
    (good,) = _fake_cli(real, _exe_names()[:1])
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(cwd), str(root), str(real)]))
    found = sb.find_cli(root)
    assert found and os.path.isabs(found) and Path(found).resolve() == good.resolve()


def test_find_cli_override_must_be_absolute_and_outside_root(tmp_path, monkeypatch, clean_cli_env):
    root, other = tmp_path / "root", tmp_path / "other"
    (inside,) = _fake_cli(root, _exe_names()[:1])
    (outside,) = _fake_cli(other, _exe_names()[:1])
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    monkeypatch.setenv("SCANOSS_PY", str(inside))
    assert sb.find_cli(root) is None
    monkeypatch.setenv("SCANOSS_PY", outside.name)
    monkeypatch.chdir(other)
    assert sb.find_cli(root) is None
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    monkeypatch.setenv("SCANOSS_PY", str(outside))
    assert Path(sb.find_cli(root)).resolve() == outside.resolve()


def test_hijack_binary_in_scan_root_is_never_run(tmp_path, monkeypatch, clean_cli_env, capsys):
    project(tmp_path)
    _fake_cli(tmp_path / "proj")
    monkeypatch.chdir(tmp_path / "proj")
    monkeypatch.setenv("PATH", str(tmp_path))
    rc = sb.main(["plan", str(tmp_path / "proj"), "--include", "src", "--out", str(tmp_path / "p.json")])
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "scanoss_not_installed"


def test_child_env_disables_cwd_lookup_and_runner_refuses_relative_argv(tmp_path):
    assert sb.child_env({"PATH": "x"})["NoDefaultCurrentDirectoryInExePath"] == "1"
    rc, out, err = sb.make_runner()(["scanoss-py", "fingerprint"], {}, str(tmp_path))
    assert rc != 0 and "absolute" in err


def test_plan_passes_absolute_cli_to_fingerprint(tmp_path, monkeypatch, clean_cli_env):
    project(tmp_path)
    real = tmp_path / "real"
    _fake_cli(real, _exe_names()[:1])
    monkeypatch.setenv("PATH", str(real))
    fr = FakeRunner()
    plan_for(tmp_path, runner=fr)
    assert all(os.path.isabs(c["argv"][0]) for c in fr.calls)


# ---- client version ---------------------------------------------------------------------------------

def test_untested_client_version_is_refused(tmp_path, capsys):
    project(tmp_path)
    for v in ("Version: 1.55.0\n", "Version: 1.53.9", "Version: 2.0.0", "garbage", ""):
        with pytest.raises(sb.ScanossError) as e:
            plan_for(tmp_path, runner=FakeRunner(version=v))
        assert e.value.code == "scanoss_version_untested"
    plan = plan_for(tmp_path, runner=FakeRunner(version="Version: 1.55.0\n"), allow_untested=True)
    text = approval.render_preview(plan)
    assert "1.55.0" in text and "1.54.x" in text
    ok = plan_for(tmp_path, runner=FakeRunner(version="Version: 1.54.7\n"))
    assert "1.54.7" in approval.render_preview(ok)
    rc = sb.main(["plan", str(tmp_path / "proj"), "--include", "src", "--out", str(tmp_path / "x.json"),
                  "--allow-untested-client"], runner=FakeRunner(version="Version: 1.60.0"))
    assert rc == 0
    capsys.readouterr()
    rc = sb.main(["plan", str(tmp_path / "proj"), "--include", "src", "--out", str(tmp_path / "y.json")],
                 runner=FakeRunner(version="Version: 1.60.0"))
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "scanoss_version_untested"


def test_requirements_pin_scanoss():
    text = (Path(__file__).parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "scanoss>=1.54,<1.55" in text


# ---- I-2: timing, plan cap, possibly_sent -------------------------------------------------------------

def test_scan_timeout_formula():
    assert sb.expected_posts("x" * 32768, 1) == 1
    assert sb.expected_posts("x" * 32769, 1) == 2
    assert sb.expected_posts("x", 65) == 3
    assert sb.scan_timeout(1) == pytest.approx(60 + 6.6 * 1 / 5 * 1.5)
    assert sb.scan_timeout(120) == pytest.approx(60 + 6.6 * 120 / 5 * 1.5)
    assert sb.scan_timeout(10 ** 6) == 900


def test_plan_over_post_budget_is_rejected(tmp_path):
    project(tmp_path)
    big = "file=" + "a" * 32 + ",0,1.js\nfh2=" + "b" * 32 + "\n" + "".join(
        f"{i}=0123abcd,4567ef01\n" for i in range(1, 220_000))
    assert 120 * 32768 < len(big) < sb.MAX_WFP_BYTES
    with pytest.raises(sb.ScanossError) as e:
        plan_for(tmp_path, runner=FakeRunner(fp_text=big))
    assert e.value.code == "plan_too_large" and "--include" in str(e.value)


def test_run_builds_a_scan_runner_with_the_scaled_budget(tmp_path, monkeypatch, clean_cli_env):
    plan, path = run_setup(tmp_path)
    cli = tmp_path / "bin"
    _fake_cli(cli, _exe_names()[:1])
    monkeypatch.setenv("PATH", str(cli))
    seen = {}

    def spy(total=sb.TOTAL_TIMEOUT, per_call=sb.CALL_TIMEOUT):
        seen["args"] = (total, per_call)
        return noop_scan

    monkeypatch.setattr(sb, "make_runner", spy)
    sb.run(path, plan["sha256"])
    posts = sb.expected_posts(plan["requests"][0]["body"], len(plan["local_only"]["id_map"]))
    assert seen["args"][1] == pytest.approx(sb.scan_timeout(posts))


def test_timeout_after_start_is_possibly_sent(tmp_path):
    plan, path = run_setup(tmp_path)
    res = sb.run(path, plan["sha256"], runner=FakeRunner(scan=lambda *a: (124, "", "\ntimeout after 60s")))
    assert res["errors"][0]["code"] == "backend_failed" and res["not_sent"] == []
    assert res["requests_sent"][0]["status"] == "possibly_sent"


def test_failure_to_start_is_not_sent(tmp_path):
    plan, path = run_setup(tmp_path)
    res = sb.run(path, plan["sha256"],
                 runner=FakeRunner(scan=lambda *a: (127, "", "cannot start the command: OSError")))
    assert res["not_sent"] and res["requests_sent"][0]["status"] != "possibly_sent"


def test_preview_shows_expected_request_count(tmp_path):
    project(tmp_path, n=70)
    plan = plan_for(tmp_path)
    assert any("70 files, 3 expected upload requests" in d for d in plan["disclosure"])
    assert "3 expected upload requests" in approval.render_preview(plan)


# ---- M-1: ASCII only -----------------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "file=" + "a" * 32 + ",0,\u0661.js", "file=" + "a" * 32 + ",0,\uff11.js", "file=" + "a" * 32 + ",\u0661,1.js",
    "file=" + "a" * 32 + ",0,1.JS", "file=" + "a" * 32 + ",0,1.\u00e9s"])
def test_unicode_digits_and_uppercase_extensions_rejected(line):
    with pytest.raises(ValueError):
        sb.sanitise_wfp(line + "\n")
    with pytest.raises(ValueError):
        sb.describe_wfp(line + "\n")


def test_unicode_digit_snippet_lines_rejected():
    head = "file=" + "a" * 32 + ",0,1.js\n"
    for snip in ("\u0663=0123abcd", "3=0123abc\u0661", "\uff13=0123abcd", "3=0123ABCD"):
        for fn in (sb.sanitise_wfp, sb.describe_wfp):
            with pytest.raises(ValueError):
                fn(head + snip + "\n")


def test_uppercase_source_extension_is_staged_lowercase(tmp_path):
    root = mk(tmp_path / "proj", {"src/Upper.JS": js(1), "src/Mixed.Py": "x = 1\n" * 3})
    plan = sb.make_plan(root, ["src"], runner=FakeRunner())
    assert sorted(plan["local_only"]["id_map"]) in (["1.js", "2.py"], ["1.py", "2.js"])
    assert "JS" not in plan["requests"][0]["body"] and "Upper" not in plan["requests"][0]["body"]


def test_stage_rejects_odd_extensions(tmp_path):
    from inventory import SourceFile
    p = tmp_path / "a.js"
    p.write_text("x = 1\n")
    dest = tmp_path / "d"
    dest.mkdir()
    for rel in ("a.j s", "a.\u00e9s", "a." + "x" * 17, "a"):
        with pytest.raises(ValueError, match="bad_extension"):
            sb.stage([SourceFile(rel, p, "c_like", 6)], dest)
    sb.stage([SourceFile("A.JS", p, "c_like", 6)], dest)
    assert (dest / "1.js").exists()


# ---- M-2: strict response handling ----------------------------------------------------------------------

def _entry(**kw):
    e = {"id": "snippet", "matched": "90%", "lines": "1-20", "url": "https://github.com/x/y", "purl": [],
         "vendor": "v", "version": "1"}
    e.update(kw)
    return e


@pytest.mark.parametrize("matched", ["NaN%", "inf%", "-5%", "101%", "abc", "", None, 80, "1e3%", "80"])
def test_unparseable_matched_is_dropped(matched):
    e = _entry(matched=matched)
    if matched is None:
        del e["matched"]
    matches, dropped, _ = sb.parse_scan({"1.js": [e]}, {"1.js": "a.js"}, {"1.js": 30})
    assert matches == [] and dropped[0]["reason"] == "bad_matched"


def test_file_match_without_matched_still_counts():
    e = {"id": "file", "lines": "all", "url": "https://github.com/x/y", "purl": []}
    matches, _, _ = sb.parse_scan({"1.js": [e]}, {"1.js": "a.js"}, {"1.js": 30})
    assert len(matches) == 1


def test_control_characters_and_ansi_are_stripped_and_strings_capped():
    evil = "ev\x1b[31mil\x1b[0m\nname\r\x00\x07" + "z" * 900
    e = _entry(vendor=evil, version="1\x1b]0;title\x07.0", licenses=[{"name": "MIT\x1b[1m\n", "source": "x"}],
               release_date="2020\n-01")
    (m,), _, _ = sb.parse_scan({"1.js": [e]}, {"1.js": "a.js"}, {"1.js": 30})
    c = m.candidate
    blob = json.dumps([c.author, c.revision, c.licenses, c.date])
    for bad in ("\\u001b", "\\n", "\\r", "\\u0000", "\\u0007"):
        assert bad not in blob
    assert c.author.startswith("evilname") and len(c.author) <= 500
    assert c.licenses[0]["spdx"] == "MIT"


def test_dropped_list_is_capped_and_counted(tmp_path, monkeypatch):
    plan, path = run_setup(tmp_path)
    monkeypatch.setattr(sb, "MAX_DROPPED", 5)
    many = {"1.js": [_entry(matched="5%") for _ in range(sb.MAX_ENTRIES_PER_FILE)]}
    many.update({f"x{i}.js": [] for i in range(30)})

    def scan(argv, env, cwd):
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps(many), encoding="utf-8")
        return 0, "", ""

    res = sb.run(path, plan["sha256"], runner=FakeRunner(scan=scan))
    assert len(res["dropped"]) == 5 and res["dropped_truncated"] > 0


def test_results_are_size_capped(tmp_path, monkeypatch):
    plan, path = run_setup(tmp_path)
    monkeypatch.setattr(sb, "MAX_RESULTS_BYTES", 20_000)
    ents = [_entry(lines=f"{i}-{i + 9}", vendor="v" * 400) for i in range(1, 20)]

    def scan(argv, env, cwd):
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps({"1.js": ents * 1}), encoding="utf-8")
        return 0, "", ""

    # more matches than fit: the file has 12 lines so use many spans per entry instead
    ents = [_entry(lines=",".join(f"{i}-{i + 8}" for i in range(1, 12)), vendor="v" * 400)
            for _ in range(sb.MAX_ENTRIES_PER_FILE)]
    res = sb.run(path, plan["sha256"], runner=FakeRunner(scan=scan))
    assert any(e["code"] == "results_truncated" for e in res["errors"])
    assert len(json.dumps(res)) <= 20_000 + 8_000


# ---- M-3: scrub -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,gone", [
    (r"ERROR at C:\Users\Alvaro B\proj dir\a.js line 3", ("Alvaro", "proj dir")),
    (r"open 'C:\Users\Álvaro Bernal\p r o j\a.js' failed", ("Álvaro", "p r o j")),
    (r"\\server\share name\x y.py", ("share name", "server")),
    ("Authorization: Bearer abc123SECRET", ("abc123SECRET",)),
    ("sent Bearer abc123SECRET to", ("abc123SECRET",)),
    ("proxy http://bob:hunter2@proxy.corp:8080 failed", ("hunter2", "bob")),
    ("x-api-key: sk-live-123", ("sk-live-123",)),
    ("GET /x?token=abcd1234&key=zzz9&a=1", ("abcd1234", "zzz9")),
    ("file:///C:/Users/x/a.py", ("Users",)),
    ("/home/alvaro/my proj/a.py", ("alvaro", "my proj")),
])
def test_scrub_examples(text, gone):
    out = sb.scrub(text)
    for g in gone:
        assert g not in out, out


def test_scrub_keeps_service_url():
    out = sb.scrub("ERROR: The SCANOSS API request failed for https://api.osskb.org/scan/direct")
    assert "https://api.osskb.org/scan/direct" in out


# ---- M-5: output cap enforced while the process runs -----------------------------------------------------------

def test_default_runner_kills_a_flooding_child(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "MAX_OUTPUT_BYTES", 200_000)
    code = "import sys\nwhile True:\n    sys.stdout.write('x' * 100000); sys.stdout.flush()\n"
    t = time.monotonic()
    rc, out, err = sb.make_runner(total=60, per_call=60)([sys.executable, "-c", code], dict(os.environ),
                                                         str(tmp_path))
    assert time.monotonic() - t < 20 and rc != 0 and "output" in err.lower()
    assert len(out) <= 200_000


def test_default_runner_kills_a_child_flooding_the_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "MAX_OUTPUT_BYTES", 200_000)
    code = ("import time\nf = open('big.out', 'wb')\n"
            "while True:\n    f.write(b'x' * 100000); f.flush(); time.sleep(0.01)\n")
    rc, out, err = sb.make_runner(total=60, per_call=60)([sys.executable, "-c", code], dict(os.environ),
                                                         str(tmp_path))
    assert rc != 0 and "output" in err.lower()


# ---- M-6: hardlinks ---------------------------------------------------------------------------------------------

def test_hardlinked_file_is_excluded(tmp_path):
    root = mk(tmp_path / "proj", {"src/ok.js": js(1), "src/orig.js": js(2)})
    outside = tmp_path / "outside.js"
    outside.write_text(js(9), encoding="utf-8")
    try:
        os.link(outside, root / "src" / "linked.js")
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hardlinks not available")
    plan = sb.make_plan(root, ["src"], runner=FakeRunner())
    ex = {e["rel"]: e["reason"] for e in plan["excluded"]}
    assert ex["src/linked.js"] == "hardlink"
    assert sorted(plan["local_only"]["id_map"].values()) == ["src/ok.js", "src/orig.js"]


# ---- mutation survivors ---------------------------------------------------------------------------------------------

def test_env_filter_is_case_insensitive_in_plan_and_run(tmp_path, monkeypatch):
    names = ("Github_Token", "my_Secret", "ScanOSS_api_key", "Db_PassWord", "PGPASS", "Session_Cookie")
    for n in names:
        monkeypatch.setenv(n, "v")
    monkeypatch.setenv("Https_Proxy", "http://proxy.example:3128")
    plan, path = run_setup(tmp_path)
    fr = FakeRunner(scan=noop_scan)
    sb.run(path, plan["sha256"], runner=fr)
    plan_for(tmp_path, runner=fr)
    assert len(fr.calls) >= 3
    for c in fr.calls:
        assert not {n.lower() for n in names} & {k.lower() for k in c["env"]}
        assert {k.lower(): v for k, v in c["env"].items()}["https_proxy"] == "http://proxy.example:3128"


def test_run_uses_the_plan_body_not_the_current_files(tmp_path):
    plan, path = run_setup(tmp_path)
    for p in (tmp_path / "proj" / "src").iterdir():
        p.write_text("changed after the plan was approved\n" * 40, encoding="utf-8")
    seen = {}

    def scan(argv, env, cwd):
        seen["wfp"] = Path(argv[argv.index("--wfp") + 1]).read_bytes()
        return noop_scan(argv, env, cwd)

    fr = FakeRunner(scan=scan)
    sb.run(path, plan["sha256"], runner=fr)
    assert seen["wfp"] == plan["requests"][0]["body"].encode("utf-8")
    assert [c["argv"][1] for c in fr.calls] == ["scan"]


def test_child_env_filter_ignores_case_on_an_explicit_environment():
    env = {"Github_Token": "t", "my_Secret": "s", "ScanOSS_api_key": "k", "Db_PassWord": "p", "PgPass": "x",
           "Session_Cookie": "c", "Path": "p", "Https_Proxy": "http://proxy:1", "HOME": "h"}
    kept = sb.child_env(env)
    assert set(kept) == {"Path", "Https_Proxy", "HOME", "NoDefaultCurrentDirectoryInExePath"}
