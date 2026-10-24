"""Fingerprint tier: SCANOSS WFP backend. The privacy-critical tests come first."""
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"))

import approval  # noqa: E402
import scanoss_backend as sb  # noqa: E402
from replay import ScanossReplayRunner  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
BY_MD5 = FIXTURES / "responses" / "scanoss_by_md5.json"
CASES = FIXTURES / "cases"


# ---- helpers ------------------------------------------------------------------------

def mk(root, files):
    for rel, content in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return Path(root)


def js(n=0, lines=12):
    return "".join(f"function fn{n}_{i}(a, b) {{ return a * {i} + b - {n}; }}\n" for i in range(lines))


def fake_wfp(stage, crlf=False, extra=None, size_zero=False):
    out = []
    for p in sorted(Path(stage).iterdir()):
        data = p.read_bytes()
        flipped = data.replace(b"\r\n", b"\n") if b"\r\n" in data else data.replace(b"\n", b"\r\n")
        out.append(f"file={hashlib.md5(data).hexdigest()},{0 if size_zero else len(data)},{p.name}")
        out.append(f"fh2={hashlib.md5(flipped).hexdigest()}")
        for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if i % 3 == 0 and line.strip():
                out.append(f"{i}={zlib.crc32(line.encode()):08x}")
    out += extra or []
    return ("\r\n" if crlf else "\n").join(out) + ("\r\n" if crlf else "\n")


class FakeRunner:
    """Dispatches on argv[1]: `fingerprint` writes a WFP from the staged dir, `scan` answers from a replay."""

    def __init__(self, scan=None, crlf=False, extra=None, fp_rc=0, fp_text=None, version="Version: 1.54.2\n"):
        self.scan, self.crlf, self.extra, self.fp_rc, self.fp_text = scan, crlf, extra, fp_rc, fp_text
        self.version = version
        self.calls = []
        self.fp_dirs = []

    def __call__(self, argv, env, cwd):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "cwd": str(cwd)})
        if argv[1] == "--version":
            return 0, self.version, ""
        if argv[1] == "fingerprint":
            stage = Path(argv[2])
            self.fp_dirs.append(stage)
            if self.fp_rc:
                return self.fp_rc, "", "boom"
            text = self.fp_text if self.fp_text is not None else fake_wfp(stage, self.crlf, self.extra)
            Path(argv[argv.index("-o") + 1]).write_bytes(text.encode("utf-8"))
            return 0, "", ""
        return self.scan(argv, env, cwd)


def project(tmp_path, n=3, prefix="src"):
    return mk(tmp_path / "proj", {f"{prefix}/m{i}.js": js(i) for i in range(n)})


def plan_for(tmp_path, includes=("src",), runner=None, **kw):
    root = tmp_path / "proj"
    return sb.make_plan(root, list(includes), runner=runner or FakeRunner(), **kw)


def write(tmp_path, plan, name="plan.json"):
    p = tmp_path / name
    approval.write_plan(plan, p)
    return p


# ---- not installed / CLI shape -------------------------------------------------------

def test_not_installed_error(tmp_path, monkeypatch, capsys):
    project(tmp_path)
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("SCANOSS_PY", raising=False)
    rc = sb.main(["plan", str(tmp_path / "proj"), "--include", "src", "--out", str(tmp_path / "p.json")])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["error"] == "scanoss_not_installed"
    assert "pip install scanoss" in out["detail"]
    assert not (tmp_path / "p.json").exists()


def test_include_is_required(tmp_path, capsys):
    project(tmp_path)
    with pytest.raises(sb.ScanossError) as e:
        sb.make_plan(tmp_path / "proj", [], runner=FakeRunner())
    assert e.value.code == "include_required"
    rc = sb.main(["plan", str(tmp_path / "proj"), "--out", str(tmp_path / "p.json")],
                 runner=FakeRunner())
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "include_required"


def test_max_files_cannot_exceed_500(tmp_path):
    project(tmp_path)
    with pytest.raises(sb.ScanossError) as e:
        plan_for(tmp_path, max_files=501)
    assert e.value.code == "max_files_too_large"
    with pytest.raises(sb.ScanossError):
        plan_for(tmp_path, max_files=0)


# ---- stage / sanitise ------------------------------------------------------------------

def test_stage_neutral_names(tmp_path):
    root = mk(tmp_path / "proj", {"src/a.js": "x = 1\n", "lib/b.py": "y = 2\n"})
    from inventory import inventory
    inv = inventory(root)
    files = sorted(inv.sources, key=lambda s: s.rel)
    dest = tmp_path / "stage"
    dest.mkdir()
    mapping = sb.stage([next(s for s in files if s.rel == "src/a.js"),
                        next(s for s in files if s.rel == "lib/b.py")], dest)
    assert mapping == {"1.js": "src/a.js", "2.py": "lib/b.py"}
    assert sorted(p.name for p in dest.iterdir()) == ["1.js", "2.py"]
    assert (dest / "1.js").read_bytes() == b"x = 1\n"


def test_sanitise_zeroes_size_and_rejects_paths():
    assert sb.sanitise_wfp("file=m,450,1.js\n") == "file=m,0,1.js\n"
    for bad in ("file=m,450,src/a.js", "file=m,450,..\\a.js", "file=m,450,C:\\a.js", "file=m,450,a.js",
                "file=m,450,1.js/x", "file=m,450,/etc/1.js", "file=m,450,1.j s", "file=m,450,1.js,2"):
        with pytest.raises(ValueError, match="unsanitised_path"):
            sb.sanitise_wfp(bad + "\n")


def test_sanitise_rejects_unknown_lines():
    for bad in ("path=/home/x/a.js", "hpsm=abc", "# comment", "foo", "", "3=zz", "3=abcd1234,xyz",
                "fh2=not hex", "file=m,450", "file=m,-1,1.js", "file=a/b,1,1.js"):
        with pytest.raises(ValueError):
            sb.sanitise_wfp("file=m,1,1.js\n" + bad + "\n")


def test_sanitise_normalises_crlf():
    out = sb.sanitise_wfp("file=m,9,1.js\r\nfh2=abcd\r\n3=0123abcd,4567ef01\r\n")
    assert "\r" not in out and out == "file=m,0,1.js\nfh2=abcd\n3=0123abcd,4567ef01\n"


def test_describe_wfp():
    wfp = ("file=" + "a" * 32 + ",0,1.js\nfh2=" + "b" * 32 + "\n3=0123abcd,4567ef01\n9=89abcdef\n"
           "file=" + "c" * 32 + ",0,2.py\n")
    d = sb.describe_wfp(wfp)
    assert d == [
        {"id": "1.js", "md5": "a" * 32, "fh2": "b" * 32, "snippet_lines": [3, 9], "hash_count": 3},
        {"id": "2.py", "md5": "c" * 32, "fh2": None, "snippet_lines": [], "hash_count": 0},
    ]


# ---- the privacy gate ------------------------------------------------------------------

def test_plan_body_has_no_paths_names_or_source_text(tmp_path):
    root = tmp_path / "proj"
    mk(root, {"src/C1_so_2019_cc4.js": (CASES / "C1" / "C1_so_2019_cc4.js").read_bytes(),
              "lib/C5_ansible_unfrackpath_GPL3.py": (CASES / "C5" / "C5_ansible_unfrackpath_GPL3.py").read_bytes()})
    plan = sb.make_plan(root, ["src", "lib"], runner=FakeRunner())
    (req,) = plan["requests"]
    body = req["body"]
    assert req["method"] == "POST" and req["url"] == sb.ENDPOINT
    for rel in ("src/C1_so_2019_cc4.js", "lib/C5_ansible_unfrackpath_GPL3.py"):
        assert rel not in body
    for name in ("C1_so_2019_cc4", "C5_ansible", "unfrackpath", "src/", "lib/", "proj"):
        assert name not in body
    for f in (CASES / "C1" / "C1_so_2019_cc4.js", CASES / "C5" / "C5_ansible_unfrackpath_GPL3.py"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if len(line.strip()) > 8:
                assert line.strip() not in body
    sizes = re.findall(r"^file=([0-9a-f]{32}),(\d+),(\S+)$", body, re.M)
    assert sorted((s, n) for _, s, n in sizes) == [("0", "1.py"), ("0", "2.js")]
    assert not re.search(r"^file=[^,]*,[1-9]", body, re.M)
    assert plan["local_only"]["id_map"] == {"1.py": "lib/C5_ansible_unfrackpath_GPL3.py",
                                            "2.js": "src/C1_so_2019_cc4.js"}
    # md5 and fh2 are part of the disclosure; nothing path-like at all
    assert "/" not in body and "\\" not in body


def test_body_sizes_are_zero_even_if_fingerprinter_writes_real_ones(tmp_path):
    project(tmp_path)
    plan = plan_for(tmp_path)
    body = plan["requests"][0]["body"]
    assert re.findall(r"^file=[0-9a-f]{32},(\d+),", body, re.M) == ["0"] * 3
    # the fake wrote real sizes (see fake_wfp); the plan holds none of them
    stage_sizes = {len(js(i).encode()) for i in range(3)}
    assert not any(f",{n}," in body for n in stage_sizes)


def test_plan_only_selected_files_and_never_sensitive(tmp_path):
    root = mk(tmp_path / "proj", {
        "src/ok.js": js(1),
        "src/.env": "SECRET=1\n",
        "src/server.pem": "-----BEGIN-----\n",
        "src/node_modules/x.js": js(2),
        "src/blob.bin": b"\x00\x01\x02" * 100,
        "src/big.js": "var a = 1;\n" * 200_000,
        "src/latin.js": b"var a = '\xe9\xe8';\n" * 20,
        "src/gen.js": "// @generated\n" + js(3),
        "other/unselected.js": js(4),
    })
    fr = FakeRunner()
    plan = sb.make_plan(root, ["src"], runner=fr)
    assert len(fr.fp_dirs) == 1
    assert sorted(plan["local_only"]["id_map"].values()) == ["src/ok.js"]
    reasons = {e["rel"]: e["reason"] for e in plan["excluded"]}
    assert reasons["src/.env"] == "sensitive"
    assert reasons["src/server.pem"] == "sensitive"
    assert reasons["src/node_modules/x.js"] == "vendored"
    assert reasons["src/blob.bin"] == "binary"
    assert reasons["src/big.js"] == "too_large"
    assert reasons["src/latin.js"] == "non_utf8"
    assert reasons["src/gen.js"] == "generated"
    assert "other/unselected.js" not in reasons
    body = plan["requests"][0]["body"]
    assert body.count("file=") == 1


def test_plan_caps_files(tmp_path):
    root = mk(tmp_path / "proj", {f"src/f{i:03d}.js": f"var v{i} = {i};\n" for i in range(201)})
    plan = sb.make_plan(root, ["src"], runner=FakeRunner())
    assert len(plan["local_only"]["id_map"]) == 200
    assert plan["cost"]["files"] == 200
    capped = [e for e in plan["excluded"] if e["reason"] == "cap"]
    assert [e["rel"] for e in capped] == ["src/f200.js"]
    assert plan["requests"][0]["body"].count("file=") == 200
    plan2 = sb.make_plan(root, ["src"], runner=FakeRunner(), max_files=5)
    assert len(plan2["local_only"]["id_map"]) == 5
    assert sum(e["reason"] == "cap" for e in plan2["excluded"]) == 196


def test_numeric_looking_root_file_cannot_collide_with_neutral_names(tmp_path):
    files = {f"src/f{i:03d}.js": f"var v{i} = {i};\n" for i in range(120)}
    files["100.js"] = "var x = 100;\n"
    root = mk(tmp_path / "proj", files)
    plan = sb.make_plan(root, [".", "src"], runner=FakeRunner())
    ex = {e["rel"]: e["reason"] for e in plan["excluded"]}
    assert ex["100.js"] == "bad_name"
    assert "100.js" not in plan["local_only"]["id_map"].values()


def test_hostile_names_are_excluded_not_fatal(tmp_path):
    root = mk(tmp_path / "proj", {"src/ok.js": js(1), "src/a=/tmp/x.js": js(2)})
    plan = sb.make_plan(root, ["src"], runner=FakeRunner())
    assert list(plan["local_only"]["id_map"].values()) == ["src/ok.js"]
    assert any(e["reason"] == "bad_name" for e in plan["excluded"])
    assert "a=/tmp" not in json.dumps(plan["excluded"])


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_symlink_outside_root_is_never_staged(tmp_path):
    outside = tmp_path / "outside.js"
    outside.write_text(js(9), encoding="utf-8")
    root = mk(tmp_path / "proj", {"src/ok.js": js(1)})
    try:
        os.symlink(outside, root / "src" / "link.js")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    fr = FakeRunner()
    plan = sb.make_plan(root, ["src"], runner=fr)
    assert list(plan["local_only"]["id_map"].values()) == ["src/ok.js"]
    assert {e["rel"]: e["reason"] for e in plan["excluded"]}["src/link.js"] == "symlink"


def test_crlf_fingerprint_output_is_sent_as_lf(tmp_path):
    project(tmp_path)
    plan = plan_for(tmp_path, runner=FakeRunner(crlf=True))
    assert "\r" not in plan["requests"][0]["body"]


@pytest.mark.parametrize("extra", [
    ["path=/home/me/secret/a.js"], ["hpsm=abc"], ["file=" + "a" * 32 + ",5,src/a.js"],
    ["file=" + "a" * 32 + ",5,99.js"],
])
def test_unsanitised_fingerprint_output_aborts(tmp_path, extra):
    project(tmp_path)
    with pytest.raises(sb.ScanossError) as e:
        plan_for(tmp_path, runner=FakeRunner(extra=extra))
    assert e.value.code == "unsanitised_wfp"


def test_oversize_wfp_is_rejected(tmp_path):
    project(tmp_path)
    huge = "file=" + "a" * 32 + ",0,1.js\n" + ("3=0123abcd\n" * 800_000)
    assert len(huge) > sb.MAX_WFP_BYTES
    with pytest.raises(sb.ScanossError) as e:
        plan_for(tmp_path, runner=FakeRunner(fp_text=huge))
    assert e.value.code == "plan_too_large"


def test_fingerprint_failure_is_reported(tmp_path):
    project(tmp_path)
    with pytest.raises(sb.ScanossError) as e:
        plan_for(tmp_path, runner=FakeRunner(fp_rc=1))
    assert e.value.code == "fingerprint_failed"


def test_fingerprint_temp_dir_is_deleted(tmp_path):
    project(tmp_path)
    fr = FakeRunner()
    plan_for(tmp_path, runner=fr)
    assert fr.fp_dirs and not fr.fp_dirs[0].exists() and not fr.fp_dirs[0].parent.exists()
    bad = FakeRunner(fp_rc=1)
    with pytest.raises(sb.ScanossError):
        plan_for(tmp_path, runner=bad)
    assert not bad.fp_dirs[0].parent.exists()


def test_fingerprint_child_gets_sanitised_env_and_temp_cwd(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.setenv("SCANOSS_API_KEY", "k")
    monkeypatch.setenv("MY_SECRET_THING", "s")
    fr = FakeRunner()
    plan_for(tmp_path, runner=fr)
    c = next(x for x in fr.calls if x["argv"][1] == "fingerprint")
    assert "SCANOSS_API_KEY" not in c["env"] and "MY_SECRET_THING" not in c["env"]
    assert Path(c["cwd"]).resolve() != (tmp_path / "proj").resolve()
    assert str(tmp_path / "proj") not in " ".join(c["argv"])
    assert c["argv"][1] == "fingerprint" and "--skip-settings-file" in c["argv"]


def test_preview_shows_wfp_and_disclosure(tmp_path):
    project(tmp_path)
    plan = plan_for(tmp_path)
    text = approval.render_preview(plan)
    assert plan["requests"][0]["body"].strip() in text
    for line in sb.DISCLOSURE:
        assert line in text
    assert "designed for professional use only" in text
    assert "https://www.softwaretransparency.org/terms" in text
    assert "3 files" in text
    assert plan["tier"] == "fingerprint" and plan["backend"] == "scanoss"


def test_disclosure_is_exact():
    d = sb.DISCLOSURE
    assert len(d) == 6
    assert d[0].startswith("This uploads one fingerprint file (WFP) to the Software Transparency Foundation's "
                           "free SCANOSS service at api.osskb.org, which may log usage to monitor service load; "
                           "no retention period is published.")
    assert "an MD5 hash of the whole file, an MD5 hash of the file with its line endings flipped" in d[1]
    assert "File names, paths and sizes are not sent" in d[2] and "sizes are set to 0" in d[2]
    assert "client name and version (scanoss-py)" in d[3]
    assert d[4].startswith("Free-tier limits: 5000 files/day, 7500/week, 15000/month. This plan uses N files. "
                           "The service's terms allow software identification only.")
    assert '"designed for professional use only and not offered for end-consumer use"' in d[4]
    assert d[5].startswith("The client uploads the fingerprint file in several smaller POST requests (about one "
                           "per 32 files or 32 KB), each with its own random request id, plus the headers "
                           "user-agent scanoss-py/<version>, x-scanoss-client (tool, version, build date), and a "
                           "form field format=plain; N requests are expected for this plan.")


# ---- run ---------------------------------------------------------------------------------

def run_setup(tmp_path, runner=None):
    project(tmp_path)
    plan = plan_for(tmp_path)
    return plan, write(tmp_path, plan)


def test_run_invokes_cli_with_approved_wfp_only(tmp_path, monkeypatch):
    plan, path = run_setup(tmp_path)
    monkeypatch.setenv("SCANOSS_API_KEY", "leak-me")
    monkeypatch.setenv("SCANOSS_SCAN_URL", "https://evil.example/scan")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    seen = {}

    def scan(argv, env, cwd):
        seen.update(argv=list(argv), env=dict(env), cwd=cwd)
        seen["wfp"] = Path(argv[argv.index("--wfp") + 1]).read_bytes()
        Path(argv[argv.index("-o") + 1]).write_text("{}", encoding="utf-8")
        seen["dir"] = Path(cwd)
        return 0, "", ""

    res = sb.run(path, plan["sha256"], runner=FakeRunner(scan=scan))
    argv = seen["argv"]
    i = argv.index("scan")
    assert argv[i + 1] == "--wfp"
    assert argv[i + 3:i + 7] == ["--apiurl", "https://api.osskb.org", "--skip-settings-file", "-o"]
    assert "--key" not in argv and "-k" not in argv
    assert seen["wfp"] == plan["requests"][0]["body"].encode("utf-8")
    env = seen["env"]
    assert "SCANOSS_API_KEY" not in env and "SCANOSS_SCAN_URL" not in env
    assert "GITHUB_TOKEN" not in env and "DB_PASSWORD" not in env
    assert env.get("HTTPS_PROXY") == "http://proxy.example:3128"
    assert Path(seen["cwd"]).resolve() != (tmp_path / "proj").resolve()
    assert not seen["dir"].exists()                      # temp dir removed
    assert res["requests_sent"][0]["method"] == "POST" and res["requests_sent"][0]["url"] == sb.ENDPOINT
    assert res["requests_sent"][0]["body_sha256"] == hashlib.sha256(seen["wfp"]).hexdigest()
    assert res["matches"] == [] and not res["errors"]


def test_run_hash_mismatch_never_invokes_cli(tmp_path):
    plan, path = run_setup(tmp_path)
    called = []
    fr = FakeRunner(scan=lambda *a: called.append(a) or (0, "", ""))
    with pytest.raises(approval.ApprovalError) as e:
        sb.run(path, "0" * 64, runner=fr)
    assert e.value.code == "plan_hash_mismatch"
    assert not called and fr.calls == []


def test_run_tampered_plan_never_invokes_cli(tmp_path):
    plan, path = run_setup(tmp_path)
    text = path.read_text(encoding="utf-8").replace('"fingerprint"', '"open"', 1)
    path.write_text(text, encoding="utf-8")
    fr = FakeRunner()
    with pytest.raises(approval.ApprovalError):
        sb.run(path, plan["sha256"], runner=fr)
    assert fr.calls == []


def test_run_refuses_an_approved_but_unsanitised_body(tmp_path):
    project(tmp_path)
    body = "file=" + "a" * 32 + ",450,1.js\nfh2=" + "b" * 32 + "\n3=0123abcd\n"      # real size
    plan = approval.build_plan("scanoss", "fingerprint", "proj",
                               [{"id": "s", "method": "POST", "url": sb.ENDPOINT, "body": body}],
                               [], sb.DISCLOSURE, {"requests": 1, "files": 1}, [],
                               {"id_map": {"1.js": "src/m0.js"}, "line_counts": {"1.js": 12}})
    path = write(tmp_path, plan)
    fr = FakeRunner()
    res = None
    with pytest.raises(sb.ScanossError) as e:
        res = sb.run(path, plan["sha256"], runner=fr)
    assert e.value.code == "unsanitised_wfp" and fr.calls == []


def test_run_refuses_a_different_endpoint(tmp_path):
    body = "file=" + "a" * 32 + ",0,1.js\n"
    plan = approval.build_plan("scanoss", "fingerprint", "proj",
                               [{"id": "s", "method": "POST", "url": "https://evil.example/scan/direct",
                                 "body": body}], [], sb.DISCLOSURE, {"requests": 1}, [],
                               {"id_map": {"1.js": "src/m0.js"}, "line_counts": {"1.js": 12}})
    path = write(tmp_path, plan)
    fr = FakeRunner()
    with pytest.raises(sb.ScanossError):
        sb.run(path, plan["sha256"], runner=fr)
    assert fr.calls == []


def test_run_checks_body_written_to_disk_matches_plan(tmp_path, monkeypatch):
    plan, path = run_setup(tmp_path)
    real = Path.write_bytes

    def evil(self, data):
        return real(self, data + b"9=deadbeef\n" if self.name == "approved.wfp" else data)

    monkeypatch.setattr(Path, "write_bytes", evil)
    fr = FakeRunner()
    with pytest.raises(sb.ScanossError) as e:
        sb.run(path, plan["sha256"], runner=fr)
    assert e.value.code == "approved_body_mismatch" and fr.calls == []


def test_run_removes_temp_dir_on_error(tmp_path):
    plan, path = run_setup(tmp_path)
    dirs = []

    def scan(argv, env, cwd):
        dirs.append(Path(cwd))
        raise RuntimeError("kaboom")

    res = sb.run(path, plan["sha256"], runner=FakeRunner(scan=scan))
    assert dirs and not dirs[0].exists()
    assert res["errors"][0]["code"] == "backend_failed"


def test_cli_failure_becomes_coverage_error(tmp_path, capsys):
    plan, path = run_setup(tmp_path)

    def quota(argv, env, cwd):
        return 1, "", "HTTPError: 429 Client Error: Too Many Requests"

    out = tmp_path / "res.json"
    rc = sb.main(["run", str(path), "--sha256", plan["sha256"], "--out", str(out)],
                 runner=FakeRunner(scan=quota))
    assert rc == 2
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["errors"][0]["code"] == "quota_exhausted" and res["matches"] == []
    assert res["plan_sha256"] == plan["sha256"] and res["backend"] == "scanoss"
    assert json.loads(capsys.readouterr().out)["error"] == "quota_exhausted"

    res2 = sb.run(path, plan["sha256"], runner=FakeRunner(scan=lambda *a: (3, "", "something odd")))
    assert res2["errors"][0]["code"] == "backend_failed" and res2["matches"] == []
    # the child had started: the service may have received data, so it is possibly_sent, never not_sent
    assert res2["not_sent"] == []
    (sent,) = res2["requests_sent"]
    assert sent["status"] == "possibly_sent" and sent["method"] == "POST" and sent["url"] == sb.ENDPOINT
    assert "may have reached" in sent["note"]
    assert res["requests_sent"][0]["status"] == "possibly_sent" and res["not_sent"] == []


def test_stderr_is_scrubbed_of_absolute_paths(tmp_path):
    plan, path = run_setup(tmp_path)

    def scan(argv, env, cwd):
        wfp = argv[argv.index("--wfp") + 1]
        return 1, "", (f"Error reading {wfp}\n  File \"C:\\Users\\bob\\x.py\", line 3\n"
                       "open('/home/bob/.config/scanoss.json')\nfrom https://api.osskb.org/scan/direct failed")

    res = sb.run(path, plan["sha256"], runner=FakeRunner(scan=scan))
    text = json.dumps(res)
    for frag in ("C:\\\\Users", "bob", "/home/", "approved.wfp", str(tmp_path)):
        assert frag not in text
    assert "https://api.osskb.org/scan/direct" in text


def test_bad_json_and_oversize_output_are_errors(tmp_path):
    plan, path = run_setup(tmp_path)

    def bad(argv, env, cwd):
        Path(argv[argv.index("-o") + 1]).write_text("not json", encoding="utf-8")
        return 0, "", ""

    res = sb.run(path, plan["sha256"], runner=FakeRunner(scan=bad))
    assert res["errors"][0]["code"] == "bad_response" and res["matches"] == []

    def big(argv, env, cwd):
        with open(argv[argv.index("-o") + 1], "wb") as fh:
            fh.truncate(sb.MAX_OUTPUT_BYTES + 1)
        return 0, "", ""

    res = sb.run(path, plan["sha256"], runner=FakeRunner(scan=big))
    assert res["errors"][0]["code"] == "bad_response"


def test_child_env_removes_secrets_keeps_proxies(monkeypatch):
    monkeypatch.setenv("SCANOSS_API_KEY", "a")
    monkeypatch.setenv("scanoss_scan_url", "b")
    monkeypatch.setenv("SCANOSS_SOMETHING_ELSE", "x")
    for n in ("AWS_SECRET_ACCESS_KEY", "NPM_TOKEN", "my_password", "SSH_PASSPHRASE_KEY"):
        monkeypatch.setenv(n, "v")
    monkeypatch.setenv("HTTP_PROXY", "p")
    monkeypatch.setenv("NO_PROXY", "n")
    env = sb.child_env()
    for n in ("SCANOSS_API_KEY", "scanoss_scan_url", "SCANOSS_SOMETHING_ELSE", "AWS_SECRET_ACCESS_KEY",
              "NPM_TOKEN", "my_password", "SSH_PASSPHRASE_KEY"):
        assert n not in env
    assert env["HTTP_PROXY"] == "p" and env["NO_PROXY"] == "n"


def test_default_runner_enforces_total_budget(tmp_path):
    import sys as _s
    runner = sb.make_runner(total=0.2, per_call=0.2)
    t = time.monotonic()
    rc, out, err = runner([_s.executable, "-c", "import time; time.sleep(10)"], {"PATH": os.environ.get("PATH", "")},
                          str(tmp_path))
    assert time.monotonic() - t < 5 and rc != 0 and "timeout" in err.lower()
    rc, out, err = runner([_s.executable, "-c", "print('hi')"], dict(os.environ), str(tmp_path))
    assert rc != 0 and "budget" in err.lower()           # total budget already spent


def test_default_runner_caps_output(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "MAX_OUTPUT_BYTES", 1000)
    runner = sb.make_runner()
    rc, out, err = runner([sys.executable, "-c", "print('x' * 100000)"], dict(os.environ), str(tmp_path))
    assert rc == 0 and len(out) <= 1000


# ---- parse_scan -----------------------------------------------------------------------------

def c_plan(tmp_path, names):
    """Plan over fixture cases; returns (plan, path)."""
    root = tmp_path / "proj"
    files = {}
    for case, fname in names:
        files[f"src/{fname}"] = (CASES / case / fname).read_bytes()
    mk(root, files)
    plan = sb.make_plan(root, ["src"], runner=FakeRunner())
    return plan, write(tmp_path, plan)


def run_cases(tmp_path, names):
    plan, path = c_plan(tmp_path, names)
    res = sb.run(path, plan["sha256"], runner=FakeRunner(scan=ScanossReplayRunner(BY_MD5)))
    return plan, res


def test_parse_c1_stackoverflow_candidate(tmp_path):
    _, res = run_cases(tmp_path, [("C1", "C1_so_2019_cc4.js")])
    assert not res["errors"] and len(res["matches"]) == 1
    m = res["matches"][0]
    c = m["candidate"]
    assert m["rel"] == "src/C1_so_2019_cc4.js" and (m["start"], m["end"]) == (3, 14)
    assert c["kind"] == "stackoverflow" and c["backend"] == "scanoss"
    assert c["origin_url"] == "https://stackoverflow.com/a/58437909" and c["post_id"] == 58437909
    assert c["revision"] == "17" and c["author"] == "Scotty Jamison"
    assert c["licenses"] == [{"spdx": "CC-BY-SA-4.0", "status": "declared",
                              "evidence": "scanoss_component_declared", "confidence": "medium"}]
    assert c["matched_pct"] == 78.0 and c["purl"] == ["pkg:stackoverflow/58437909"]
    assert res["kb_version"] == "26.10.19" and res["tier"] == "fingerprint"
    assert any("candidates" in n.lower() for n in res["notes"])


def test_c2_github_copy_is_repository_candidate(tmp_path):
    _, res = run_cases(tmp_path, [("C2", "C2_so_2011_cc3.js")])
    (m,) = res["matches"]
    c = m["candidate"]
    assert c["kind"] == "repository"
    assert c["origin_url"] == "https://github.com/cloudykangaroo/cloudykangaroo"
    assert c["licenses"][0]["spdx"] == "MIT"
    assert "may itself contain a copy" in c["note"] and "origin is not established" in c["note"]
    assert (m["start"], m["end"]) == (5, 15) and m["match_type"] == "near"


def test_c7_below_threshold_dropped(tmp_path):
    _, res = run_cases(tmp_path, [("C7", "C7_boilerplate.py")])
    assert res["matches"] == []
    assert res["dropped"] and res["dropped"][0]["reason"] == "below_threshold"
    assert res["dropped"][0]["rel"] == "src/C7_boilerplate.py"


def test_c9_empty_licenses_is_none_found(tmp_path):
    _, res = run_cases(tmp_path, [("C9", "C9_nolicense_noamopt.py")])
    (m,) = res["matches"]
    assert m["candidate"]["licenses"] == [{"spdx": None, "status": "none_found",
                                           "evidence": "scanoss_other", "confidence": "low"}]


def test_file_match_covers_whole_file_and_none_is_filtered(tmp_path):
    root = tmp_path / "proj"
    mk(root, {"src/a.js": (CASES / "C6" / "node_modules" / "is-number" / "index.js").read_bytes(),
              "src/b.py": (CASES / "C8" / "C8_original.py").read_bytes()})
    plan = sb.make_plan(root, ["src"], runner=FakeRunner())
    res = sb.run(write(tmp_path, plan), plan["sha256"], runner=FakeRunner(scan=ScanossReplayRunner(BY_MD5)))
    (m,) = res["matches"]
    n = len((root / "src" / "a.js").read_text(encoding="utf-8").splitlines())
    assert m["match_type"] == "file" and (m["start"], m["end"]) == (1, n)
    assert m["candidate"]["origin_url"] == "https://github.com/jonschlinkert/is-number"
    assert res["dropped"] == []                              # id "none" is neither a match nor a drop


def test_parse_scan_unit():
    entry = {"id": "snippet", "matched": "60%", "lines": "2-12", "url": "https://github.com/x/y",
             "purl": ["pkg:github/x/y"], "licenses": [{"name": "Apache-2.0", "source": "license_file"},
                                                      {"name": "Weird", "source": "scancode"}],
             "vendor": "x", "version": "1.0", "release_date": "2020-01-01",
             "server": {"kb_version": {"daily": "26.10.19"}}}
    raw = {"1.js": [entry, {"id": "none"}], "2.js": "garbage", "9.js": [entry], "3.js": [{"id": "file", "lines": "all",
           "matched": "100%", "url": "javascript:alert(1)", "purl": []}]}
    matches, dropped, kb = sb.parse_scan(raw, {"1.js": "a.js", "2.js": "b.js", "3.js": "c.js"},
                                         {"1.js": 20, "2.js": 5, "3.js": 7})
    assert kb == "26.10.19"
    assert [(m.rel, m.start, m.end) for m in matches] == [("a.js", 2, 12)]
    assert [l["evidence"] for l in matches[0].candidate.licenses] == ["scanoss_license_file", "scanoss_other"]
    reasons = {(d["rel"], d["reason"]) for d in dropped}
    assert ("c.js", "no_origin_url") in reasons and ("(unknown file)", "unknown_file") in reasons


def test_parse_scan_clamps_lines_to_the_file():
    e = {"id": "snippet", "matched": "90%", "lines": "5-500", "url": "https://github.com/x/y", "purl": []}
    matches, dropped, _ = sb.parse_scan({"1.js": [e]}, {"1.js": "a.js"}, {"1.js": 40})
    assert [(m.start, m.end) for m in matches] == [(5, 40)]
    e["lines"] = "90-120"
    matches, dropped, _ = sb.parse_scan({"1.js": [e]}, {"1.js": "a.js"}, {"1.js": 40})
    assert matches == [] and dropped[0]["reason"] == "bad_lines"


# ---- hostile input / time budgets ----------------------------------------------------------

def test_describe_wfp_rejects_50mb_malformed_fast():
    junk = "x" * 50_000_000
    t = time.perf_counter()
    for fn in (sb.describe_wfp, sb.sanitise_wfp):
        with pytest.raises(ValueError, match="plan_too_large"):
            fn(junk)
    assert time.perf_counter() - t < 2


def test_describe_and_sanitise_7mb_wfp_are_linear():
    chunk = "file=" + "a" * 32 + ",0,1.js\nfh2=" + "b" * 32 + "\n" + "".join(
        f"{i}=0123abcd,4567ef01\n" for i in range(1, 60))
    wfp = "".join(chunk.replace("1.js", f"{i}.js") for i in range(1, 7_000_000 // len(chunk)))
    assert len(wfp) < sb.MAX_WFP_BYTES
    t = time.perf_counter()
    out = sb.sanitise_wfp(wfp)
    d = sb.describe_wfp(out)
    assert time.perf_counter() - t < 2.5 and len(d) > 1000


def test_malformed_wfp_under_limit_fails_fast():
    for text in ("file=" + "," * 5_000_000, "1=" + "a" * 5_000_000, "\n" * 5_000_000, "9" * 5_000_000 + "=aa"):
        t = time.perf_counter()
        with pytest.raises(ValueError):
            sb.sanitise_wfp(text)
        with pytest.raises(ValueError):
            sb.describe_wfp(text)
        assert time.perf_counter() - t < 2


def test_plan_time_budget_with_many_files(tmp_path):
    root = mk(tmp_path / "proj", {f"d{i % 20}/f{i}.js": f"var v{i} = {i};\n" * 5 for i in range(1500)})
    t = time.perf_counter()
    plan = sb.make_plan(root, ["."], runner=FakeRunner())
    assert time.perf_counter() - t < 20
    assert len(plan["local_only"]["id_map"]) == 200
    assert plan["cost"]["excluded_by_reason"]["cap"] == 1300
    assert len(plan["excluded"]) == sb.MAX_EXCLUDED_LISTED + 1
    assert plan["excluded"][-1]["reason"] == "truncated_list"


def test_huge_line_count_file(tmp_path):
    root = mk(tmp_path / "proj", {"src/lines.js": "\n" * 900_000 + "var a = 1;\n"})
    t = time.perf_counter()
    plan = sb.make_plan(root, ["src"], runner=FakeRunner())
    assert time.perf_counter() - t < 10
    assert plan["local_only"]["line_counts"]["1.js"] == 900_001


# ---- CLI -------------------------------------------------------------------------------------

def test_cli_plan_and_run_roundtrip(tmp_path, capsys):
    project(tmp_path)
    out_plan, out_res = tmp_path / "plan.json", tmp_path / "res.json"
    rc = sb.main(["plan", str(tmp_path / "proj"), "--include", "src", "--out", str(out_plan), "--max-files", "2"],
                 runner=FakeRunner())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["sha256"] and "Files to be fingerprinted" in out["preview"]
    assert out["files"] == [{"name": "1.js", "rel": "src/m0.js"}, {"name": "2.js", "rel": "src/m1.js"}]
    assert any(e["reason"] == "cap" for e in out["excluded"])
    rc = sb.main(["plan", str(tmp_path / "proj"), "--include", "src", "--out", str(out_plan)], runner=FakeRunner())
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "output_exists"
    rc = sb.main(["run", str(out_plan), "--sha256", out["sha256"], "--out", str(out_res)],
                 runner=FakeRunner(scan=lambda a, e, c: (Path(a[a.index("-o") + 1]).write_text("{}") and 0 or 0, "", "")))
    assert rc == 0 and json.loads(out_res.read_text(encoding="utf-8"))["plan_sha256"] == out["sha256"]


def test_cli_bad_hash_writes_error_results(tmp_path, capsys):
    project(tmp_path)
    plan = plan_for(tmp_path)
    path = write(tmp_path, plan)
    fr = FakeRunner()
    rc = sb.main(["run", str(path), "--sha256", "f" * 64, "--out", str(tmp_path / "r.json")], runner=fr)
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "plan_hash_mismatch"
    assert fr.calls == [] and json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["errors"]


def test_include_errors_propagate(tmp_path, capsys):
    project(tmp_path)
    rc = sb.main(["plan", str(tmp_path / "proj"), "--include", "nope", "--out", str(tmp_path / "p.json")],
                 runner=FakeRunner())
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "include_not_found"
    rc = sb.main(["plan", str(tmp_path / "proj"), "--include", "..", "--out", str(tmp_path / "p.json")],
                 runner=FakeRunner())
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "include_outside_root"


def test_windows_style_include_and_crlf_bom(tmp_path):
    root = mk(tmp_path / "proj", {"src/lib/a.js": b"\xef\xbb\xbfvar a = 1;\r\nvar b = 2;\r\n"})
    plan = sb.make_plan(root, ["src\\lib"], runner=FakeRunner())
    assert plan["local_only"]["id_map"] == {"1.js": "src/lib/a.js"}
    assert plan["local_only"]["line_counts"] == {"1.js": 2}


def test_symlink_checks_without_real_symlinks(tmp_path, monkeypatch):
    """Windows may forbid symlinks: exercise both checks (is_symlink and realpath escaping the root)."""
    root = mk(tmp_path / "proj", {"src/ok.js": js(1), "src/via_link.js": js(2), "src/escape.js": js(3)})
    real_is_symlink, real_realpath = Path.is_symlink, os.path.realpath
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "via_link.js" or real_is_symlink(self))
    outside = str(tmp_path / "elsewhere" / "x.js")
    monkeypatch.setattr(os.path, "realpath",
                        lambda p, *a, **k: outside if str(p).endswith("escape.js") else real_realpath(p, *a, **k))
    plan = sb.make_plan(root, ["src"], runner=FakeRunner())
    ex = {e["rel"]: e["reason"] for e in plan["excluded"]}
    assert ex == {"src/via_link.js": "symlink", "src/escape.js": "symlink"}
    assert list(plan["local_only"]["id_map"].values()) == ["src/ok.js"]


def test_stage_refuses_files_that_changed_into_something_unsafe(tmp_path):
    from inventory import SourceFile
    p = tmp_path / "a.js"
    p.write_bytes(b"\x00\x01binary")
    dest = tmp_path / "d"
    dest.mkdir()
    with pytest.raises(ValueError, match="binary"):
        sb.stage([SourceFile("a.js", p, "c_like", 8)], dest)
    assert list(dest.iterdir()) == []


def test_run_consults_the_guard_before_invoking_the_cli(tmp_path, monkeypatch):
    plan, path = run_setup(tmp_path)
    calls = []

    def deny(self, method, url, body, headers=None):
        calls.append((method, url, body))
        raise approval.ApprovalError("request_not_in_plan", "denied")

    monkeypatch.setattr(approval.Guard, "check", deny)
    fr = FakeRunner()
    with pytest.raises(approval.ApprovalError):
        sb.run(path, plan["sha256"], runner=fr)
    assert calls == [("POST", sb.ENDPOINT, plan["requests"][0]["body"].encode("utf-8"))]
    assert fr.calls == []
