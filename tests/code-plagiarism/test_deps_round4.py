"""Strict tier, fix round 4: multi-line (continuation) and empty values in INI-style config files."""
import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-plagiarism" / "scripts"))

import deps  # noqa: E402

NL = "\n"
ATTRS = ["pypi/pypi/-/attrs/24.1.0"]
REQ = "attrs==24.1.0" + NL
PRIV = "https://u:SECRET@pypi.corp.internal/simple"
PUB = "https://pypi.org/simple"


def mk(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8", newline="")
    return tmp_path


def sent(tmp_path, files):
    mk(tmp_path, files)
    plan = deps.make_plan(tmp_path)
    return sorted(c for r in plan["requests"] for c in json.loads(r["body"]))


def reasons_of(tmp_path):
    out = {}
    for e in deps.make_plan(tmp_path)["excluded"]:
        m = re.fullmatch(r"(\w+) \((\d+)\)", e["reason"])
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
    return out


def assert_withheld(tmp_path, files):
    assert sent(tmp_path, files) == []
    plan = deps.make_plan(tmp_path)
    assert "SECRET" not in json.dumps(plan)
    r = reasons_of(tmp_path)
    assert r.get("private_registry", 0) + r.get("private_registry_possible", 0) == 1


# ---- pip.conf / pip.ini ------------------------------------------------------------------

PIP_KEYS = ["index-url", "extra-index-url", "index_url", "extra_index_url", "Index-Url", "trusted-host"]


@pytest.mark.parametrize("fname", ["pip.conf", "pip.ini"])
@pytest.mark.parametrize("key", PIP_KEYS)
@pytest.mark.parametrize("sep", [" = ", "=", ": ", ":"])
@pytest.mark.parametrize("indent", ["    ", "\t"])
def test_pip_conf_continuation_private_withheld(tmp_path, fname, key, sep, indent):
    value = "pypi.corp.internal" if key == "trusted-host" else PRIV
    conf = "[global]" + NL + key + sep.rstrip() + NL + indent + value + NL
    assert_withheld(tmp_path, {"requirements.txt": REQ, fname: conf})


@pytest.mark.parametrize("section", ["global", "install", "download"])
def test_pip_conf_continuation_any_pip_section(tmp_path, section):
    conf = "[%s]\nindex-url =\n    %s\n" % (section, PRIV)
    assert_withheld(tmp_path, {"requirements.txt": REQ, "pip.conf": conf})


@pytest.mark.parametrize("key", ["index-url", "extra-index-url", "trusted-host"])
def test_pip_conf_empty_value_is_possible_private(tmp_path, key):
    assert sent(tmp_path, {"requirements.txt": REQ, "pip.conf": "[global]\n%s =\ntimeout = 60\n" % key}) == []
    assert reasons_of(tmp_path) == {"private_registry_possible": 1}


def test_pip_conf_empty_index_url_at_end_of_file(tmp_path):
    assert sent(tmp_path, {"requirements.txt": REQ, "pip.conf": "[global]\nindex-url ="}) == []


def test_pip_conf_second_line_of_extra_index_private(tmp_path):
    conf = "[global]\nextra-index-url = %s\n    %s\n" % (PUB, PRIV)
    assert_withheld(tmp_path, {"requirements.txt": REQ, "pip.conf": conf})


def test_pip_conf_continuation_with_comment_and_blank_lines(tmp_path):
    conf = "[global]\nindex-url =\n    # the corp mirror\n\n    %s\n" % PRIV
    assert_withheld(tmp_path, {"requirements.txt": REQ, "pip.conf": conf})


def test_pip_conf_crlf_continuation(tmp_path):
    conf = "[global]\r\nindex-url =\r\n    %s\r\n" % PRIV
    assert_withheld(tmp_path, {"requirements.txt": REQ, "pip.conf": conf})


def test_pip_conf_quoted_continuation(tmp_path):
    conf = '[global]\nindex-url =\n    "%s"\n' % PRIV
    assert_withheld(tmp_path, {"requirements.txt": REQ, "pip.conf": conf})


@pytest.mark.parametrize("conf", [
    "[global]\nindex-url =\n    %s\n" % PUB,
    "[global]\nindex-url =\n\t%s\r\n" % PUB,
    "[global]\nindex-url =\n    '%s'\n" % PUB,
    "[global]\nindex-url = %s  # main index\n" % PUB,
    "[global]\nextra-index-url =\n    %s\n    https://pypi.python.org/simple\n" % PUB,
    "[global]\nindex-url =\n    %s\ntimeout = 60\n" % PUB,
    "[global]\ntrusted-host =\n    pypi.org\n    files.pythonhosted.org\n",
])
def test_pip_conf_public_continuation_stays_public(tmp_path, conf):
    assert sent(tmp_path, {"requirements.txt": REQ, "pip.conf": conf}) == ATTRS


def test_pip_conf_next_key_is_not_a_continuation(tmp_path):
    conf = "[global]\nindex-url = %s\ntimeout = 60\n[install]\nno-deps = no\n" % PUB
    assert sent(tmp_path, {"requirements.txt": REQ, "pip.conf": conf}) == ATTRS


def test_pip_conf_find_links_continuation_still_possible(tmp_path):
    assert_withheld(tmp_path, {"requirements.txt": REQ, "pip.conf": "[global]\nfind-links =\n    ./wheels\n"})


# ---- setup.cfg ------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["index_url", "index-url", "find_links", "find-links", "Index_Url"])
@pytest.mark.parametrize("indent", ["  ", "\t"])
@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_setup_cfg_continuation_private_withheld(tmp_path, key, indent, eol):
    cfg = "[easy_install]" + eol + key + " =" + eol + indent + PRIV + eol
    assert_withheld(tmp_path, {"requirements.txt": REQ, "setup.cfg": cfg})


def test_setup_cfg_find_links_multiple_lines(tmp_path):
    cfg = "[easy_install]\nfind_links =\n    %s\n    https://wheels.corp.internal/\n" % PUB
    assert_withheld(tmp_path, {"requirements.txt": REQ, "setup.cfg": cfg})


@pytest.mark.parametrize("key", ["index_url", "find_links"])
def test_setup_cfg_empty_value_is_possible_private(tmp_path, key):
    cfg = "[easy_install]\n%s =\n[metadata]\nname = x\n" % key
    assert sent(tmp_path, {"requirements.txt": REQ, "setup.cfg": cfg}) == []
    assert reasons_of(tmp_path).get("private_registry_possible") == 1


def test_setup_cfg_public_continuation_stays_public(tmp_path):
    cfg = "[easy_install]\nindex_url =\n    %s\n" % PUB
    assert sent(tmp_path, {"requirements.txt": REQ, "setup.cfg": cfg}) == ATTRS


@pytest.mark.parametrize("cfg", [
    "[metadata]\nname = x\nindex_url =\n    https://pypi.corp.internal/simple\n",
    "[metadata]\nindex_url = https://pypi.corp.internal/simple\n",
    "[metadata]\nfind_links =\n",
    "[options.extras_require]\nindex_url = foo\n",
    "[metadata]\nlong_description =\n    index_url = https://pypi.corp.internal/simple\n",
])
def test_setup_cfg_key_in_unrelated_section_does_not_trigger(tmp_path, cfg):
    assert sent(tmp_path, {"requirements.txt": REQ, "setup.cfg": cfg}) == ATTRS


def test_setup_cfg_section_switch_resets_relevance(tmp_path):
    cfg = "[metadata]\nname = x\n[easy_install]\nindex_url =\n    %s\n" % PRIV
    assert_withheld(tmp_path, {"requirements.txt": REQ, "setup.cfg": cfg})


# ---- npm / yarn empty values -----------------------------------------------------------------

ISODD = json.dumps({"dependencies": {"is-odd": "3.0.1"}})


@pytest.mark.parametrize("fname,text", [
    (".npmrc", "registry=\n"),
    (".npmrc", "registry =   \r\n"),
    (".yarnrc", "registry\n"),
    (".yarnrc", "registry \"\"\n"),
    (".yarnrc", "\"registry\"\n"),
    (".yarnrc.yml", "npmRegistryServer:\n  \"https://npm.corp.internal\"\n"),
])
def test_npm_empty_registry_is_possible_private(tmp_path, fname, text):
    assert sent(tmp_path, {"package.json": ISODD, fname: text}) == []


@pytest.mark.parametrize("fname,text", [(".npmrc", "@corp:registry=\n"), (".yarnrc", "\"@corp:registry\"\n")])
def test_npm_empty_scope_registry_withholds_scope(tmp_path, fname, text):
    pj = json.dumps({"dependencies": {"@corp/x": "1.0.0", "is-odd": "3.0.1"}})
    assert sent(tmp_path, {"package.json": pj, fname: text}) == ["npm/npmjs/-/is-odd/3.0.1"]


def test_npm_public_registry_still_public(tmp_path):
    assert sent(tmp_path / "a", {"package.json": ISODD, ".npmrc": "registry=https://registry.npmjs.org/\n"}) \
        == ["npm/npmjs/-/is-odd/3.0.1"]
    assert sent(tmp_path / "b", {"package.json": ISODD, ".yarnrc": "registry \"https://registry.yarnpkg.com\"\n"}) \
        == ["npm/npmjs/-/is-odd/3.0.1"]


# ---- TOML-based configs: empty strings / lists already parsed by tomllib ------------------------

@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
@pytest.mark.parametrize("text", ['index-url = ""\n', 'index-url = """\nhttps://pypi.corp.internal/simple"""\n',
                                  'extra-index-url = [""]\n'])
def test_uv_toml_empty_or_multiline_strings_withheld(tmp_path, text):
    assert sent(tmp_path, {"requirements.txt": REQ, "uv.toml": text}) == []


# ---- robustness ------------------------------------------------------------------------------

def test_5000_line_continuation_value_is_fast(tmp_path):
    lines = "".join("    https://pypi.org/simple\n" for _ in range(5000))
    files = {"requirements.txt": REQ, "pip.conf": "[global]\nextra-index-url =\n" + lines,
             "sub/requirements.txt": REQ,
             "sub/setup.cfg": "[easy_install]\nfind_links =\n" + lines + "    " + PRIV + "\n"}
    mk(tmp_path, files)
    t = time.perf_counter()
    plan = deps.make_plan(tmp_path)
    assert time.perf_counter() - t < 1.0
    got = sorted(c for r in plan["requests"] for c in json.loads(r["body"]))
    assert got == ATTRS                                 # root public, sub withheld (private last line)
    assert reasons_of(tmp_path).get("private_registry_possible") == 1


@pytest.mark.parametrize("fname", ["pip.conf", "setup.cfg", ".npmrc"])
def test_binary_config_still_fails_closed(tmp_path, fname):
    manifest = {"package.json": ISODD} if fname == ".npmrc" else {"requirements.txt": REQ}
    files = dict(manifest, **{fname: b"[global]\nindex-url =\n\x00\x01\x02garbage"})
    assert sent(tmp_path, files) == []
    assert reasons_of(tmp_path).get("config_unreadable")
