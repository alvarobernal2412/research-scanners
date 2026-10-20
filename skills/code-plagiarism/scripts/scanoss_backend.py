"""Fingerprint tier: a sanitised SCANOSS WFP, sent through the `scanoss-py` CLI.

PRIVACY. `plan` stages the files YOU chose under neutral names (1.js, 2.py, ...), runs the offline
`scanoss-py fingerprint` on that temp copy and rewrites the result: every `file=<md5>,<size>,<name>`
line gets size 0, and any line that is not a plain `file=`, `fh2=` or `<line>=<hash>[,<hash>]` line
aborts the plan (`unsanitised_wfp`). The WFP text is the single request body of the plan, so the
approval gate shows it verbatim and fixes it by sha256. The id -> path map lives in `local_only`
and never leaves the machine.

`run` loads the plan with the user's approved sha256, re-checks that the body is already sanitised,
writes it byte for byte to a temp file, checks the Guard, and only then calls
`scanoss-py scan --wfp <that file> --apiurl https://api.osskb.org --skip-settings-file`. The child
runs in a temp directory with every variable that looks like a secret removed (and every SCANOSS_*
variable), with no `--key`, one attempt (`--retry 0`), a 60 s limit per call and 300 s in total,
and its output is size-capped. Its stderr is scrubbed of absolute paths before it is stored.

Sensitive, vendored, generated, minified, oversized, binary, non-UTF-8 and symlinked files are never
fingerprinted. The WFP still carries whole-file MD5, a line-ending-flipped MD5 and snippet hashes with
real line numbers; DISCLOSURE says so.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approval  # noqa: E402
from approval import ApprovalError, Guard, build_plan, load_plan, render_preview, write_plan  # noqa: E402
from inventory import (MAX_FILE_BYTES, InventoryError, SourceFile, inventory,  # noqa: E402
                       read_lines, resolve_includes)
from model import Candidate, RawMatch, new_results, raw_match_to_dict, write_results  # noqa: E402
from triage import SCANOSS_MIN_LINES, declared_tree_reason, scanoss_drop_reason  # noqa: E402

ENDPOINT = "https://api.osskb.org/scan/direct"
API_BASE = "https://api.osskb.org"
SCANOSS_MAX_FILES = 200
HARD_MAX_FILES = 500
MAX_WFP_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
CALL_TIMEOUT = 60
TOTAL_TIMEOUT = 300
MAX_EXCLUDED_LISTED = 1000
MAX_ENTRIES_PER_FILE = 20
MAX_RANGES = 20
FREE_TIER_DAILY = 5000
MAX_DROPPED = 1000
MAX_RESULTS_BYTES = 16 * 1024 * 1024
MAX_STRING = 500
TESTED_CLIENT = (1, 54)                 # wire analysis (what is uploaded) was done for scanoss-py 1.54.x
# The real client uploads the WFP in ~1 POST per POST_FILES files / POST_BYTES bytes, on UPLOAD_THREADS threads,
# measured at SECONDS_PER_POST (spike section 1.6). Scan time budget:
#   min(SCAN_MAX, SCAN_BASE + SECONDS_PER_POST * posts / UPLOAD_THREADS * SAFETY)
POST_BYTES = 32768
POST_FILES = 32
UPLOAD_THREADS = 5
SECONDS_PER_POST = 6.6
SAFETY = 1.5
SCAN_BASE = 60
SCAN_MAX = 900
MAX_EXPECTED_POSTS = 120

DISCLOSURE = [
    "This uploads one fingerprint file (WFP) to the Software Transparency Foundation's free SCANOSS service "
    "at api.osskb.org, which may log usage to monitor service load; no retention period is published.",
    "For each selected file the WFP contains: an MD5 hash of the whole file, an MD5 hash of the file with its "
    "line endings flipped, and about one 32-bit hash per few lines together with the real line numbers. It "
    "contains no source text, but anyone holding these hashes can check whether a known file or passage is "
    "present.",
    "File names, paths and sizes are not sent: files are renamed 1.ext, 2.ext, \u2026 (the extension is kept) "
    "and sizes are set to 0. The mapping back to your paths stays on this machine.",
    "The request also carries your IP address, the time, and the client name and version (scanoss-py).",
    "Free-tier limits: 5000 files/day, 7500/week, 15000/month. This plan uses N files. The service's terms "
    "allow software identification only. The service describes itself as \"designed for professional use only "
    "and not offered for end-consumer use\" and limits OSSKB output to software identification (Software "
    "Transparency Foundation terms, https://www.softwaretransparency.org/terms).",
    "The client uploads the fingerprint file in several smaller POST requests (about one per 32 files or 32 KB), "
    "each with its own random request id, plus the headers user-agent scanoss-py/<version>, x-scanoss-client "
    "(tool, version, build date), and a form field format=plain; N requests are expected for this plan.",
]

_NOT_INSTALLED = "pip install scanoss (MIT); then re-run plan"

# ---- errors -----------------------------------------------------------------------------


class ScanossError(Exception):
    """code in scanoss_not_installed|include_required|max_files_invalid|max_files_too_large|unsanitised_wfp
    |plan_too_large|fingerprint_failed|nothing_to_fingerprint|approved_body_mismatch|plan_invalid
    |output_exists|output_error|usage"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ---- WFP sanitiser / parser ------------------------------------------------------------------

_A = re.ASCII
_FILE_LINE = re.compile(r"file=([0-9A-Za-z]{1,64}),([0-9]{1,12}),([0-9]{1,6}\.[a-z0-9]{1,16})", _A)
_FH2_LINE = re.compile(r"fh2=([0-9A-Fa-f]{1,64})", _A)
_SNIP_LINE = re.compile(r"([0-9]{1,9})=([0-9a-f]{8}(?:,[0-9a-f]{8})*)", _A)
_STRICT_MD5 = re.compile(r"[0-9A-Fa-f]{32}", _A)
_NEUTRAL = re.compile(r"[0-9]{1,6}\.[A-Za-z0-9]{1,16}", _A)


def _wfp_text(wfp) -> str:
    if not isinstance(wfp, str) or not wfp:
        raise ValueError("unsanitised_wfp")
    if len(wfp) > MAX_WFP_BYTES:
        raise ValueError("plan_too_large")
    text = wfp.replace("\r\n", "\n")
    if "\r" in text:
        raise ValueError("unsanitised_wfp")
    return text[:-1] if text.endswith("\n") else text


def sanitise_wfp(wfp: str) -> str:
    """Zero every file size and accept nothing but file=/fh2=/snippet lines (LF line ends out).

    Raises ValueError("unsanitised_path") for a `file=` line whose name is not N.ext, ValueError
    ("unsanitised_wfp") for any other line, ValueError("plan_too_large") above MAX_WFP_BYTES."""
    out = []
    seen_file = False
    for line in _wfp_text(wfp).split("\n"):
        if line.startswith("file="):
            m = _FILE_LINE.fullmatch(line)
            if not m:
                raise ValueError("unsanitised_path")
            out.append(f"file={m.group(1)},0,{m.group(3)}")
            seen_file = True
        elif not seen_file:
            raise ValueError("unsanitised_wfp")
        elif line.startswith("fh2="):
            if not _FH2_LINE.fullmatch(line):
                raise ValueError("unsanitised_wfp")
            out.append(line)
        elif _SNIP_LINE.fullmatch(line):
            out.append(line)
        else:
            raise ValueError("unsanitised_wfp")
    return "\n".join(out) + "\n"


def describe_wfp(wfp: str) -> list[dict]:
    """[{id, md5, fh2, snippet_lines, hash_count}] for a sanitised WFP; strict, linear, ValueError if malformed."""
    files: list[dict] = []
    names: set = set()
    cur = None
    for line in _wfp_text(wfp).split("\n"):
        if line.startswith("file="):
            m = _FILE_LINE.fullmatch(line)
            if not m or not _STRICT_MD5.fullmatch(m.group(1)) or m.group(3) in names:
                raise ValueError("unsanitised_wfp")
            names.add(m.group(3))
            cur = {"id": m.group(3), "md5": m.group(1), "fh2": None, "snippet_lines": [], "hash_count": 0}
            files.append(cur)
        elif cur is None:
            raise ValueError("unsanitised_wfp")
        elif line.startswith("fh2="):
            if not _FH2_LINE.fullmatch(line) or len(line) != 36 or cur["fh2"] is not None:
                raise ValueError("unsanitised_wfp")
            cur["fh2"] = line[4:]
        else:
            m = _SNIP_LINE.fullmatch(line)
            if not m:
                raise ValueError("unsanitised_wfp")
            cur["snippet_lines"].append(int(m.group(1)))
            cur["hash_count"] += m.group(2).count(",") + 1
    return files


# ---- child process -----------------------------------------------------------------------------

_SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASS|CREDENTIAL|AUTH|COOKIE|SESSION|DSN", re.I)


def _inside(path, root) -> bool:
    try:
        Path(os.path.normcase(os.path.realpath(path))).relative_to(Path(os.path.normcase(os.path.realpath(root))))
        return True
    except ValueError:
        return False


def _usable_cli(candidate, root) -> str | None:
    """An absolute, existing, executable file that is not the cwd, the scan root or inside either."""
    cand = str(candidate)
    if not cand or not os.path.isabs(cand):
        return None
    try:
        real = os.path.realpath(cand)
        if not os.path.isfile(real) or (os.name != "nt" and not os.access(real, os.X_OK)):
            return None
        forbidden = [os.getcwd()] + ([str(root)] if root is not None else [])
        if any(_inside(cand, f) or _inside(real, f) for f in forbidden):
            return None
    except OSError:
        return None
    return real


def find_cli(root=None) -> str | None:
    """Locate scanoss-py by walking PATH ourselves. shutil.which also searches the current directory first on
    Windows, which would run a binary planted in the scanned repository with the staged copies of its files.
    Empty, '.' and relative PATH entries, the cwd and the scan root are skipped; only an absolute existing file
    is returned (symlinks resolved). SCANOSS_PY may name the CLI, with the same rules."""
    override = os.environ.get("SCANOSS_PY")
    if override:
        return _usable_cli(override, root)
    exts = [""]
    if os.name == "nt":
        exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e] or [".exe"]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or entry == "." or not os.path.isabs(entry):
            continue
        for ext in exts:
            found = _usable_cli(os.path.join(entry, "scanoss-py" + ext), root)
            if found:
                return found
    return None


def child_env(environ=None) -> dict:
    """The user's environment minus anything that looks like a secret (case-insensitive) and every SCANOSS_*
    variable. Proxy variables are kept so the CLI works behind a proxy. The Windows current-directory lookup
    of executables is switched off for the child."""
    env = {}
    for name, value in dict(os.environ if environ is None else environ).items():
        if _SECRETISH.search(name) or name.upper().startswith("SCANOSS"):
            continue
        env[name] = value
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    return env


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ABS_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\r\n'\"<>|]*"            # C:\Users\Name With Space\...  (to end of line/quote)
    r"|\\\\[^\r\n'\"<>|]+"                                      # \\server\share name\...
    r"|file:/+[^\r\n'\"<>|]*"                                   # file:///...
    r"|~[\\/][^\r\n'\"<>|]*"                                    # ~/...
    r"|(?<![\w:/.~-])/(?:home|Users|root|tmp|var|mnt|opt|usr|etc|private|Volumes|srv|workspace|data)/[^\r\n'\"<>|]*"
    r"|(?<![\w:/.~-])/(?:[\w.@+~-]+/)+[\w.@+~-]*")             # other /a/b/c (not the path part of a URL)
_USERINFO = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@'\"]+@")
_SECRET_HEADER = re.compile(
    r"\b(authorization|proxy-authorization|x-[\w-]*(?:key|token|secret|auth)[\w-]*|api[-_]?key|cookie)"
    r"(\s*[:=]\s*)[^\r\n'\"]*", re.I)
_BEARER = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_SECRET_PARAM = re.compile(
    r"\b((?:access[_-]?|api[_-]?|auth[_-]?)?(?:token|key|secret|password|passwd|pwd))=[^\s&'\"]+", re.I)


def scrub(text, extra=()) -> str:
    """Remove absolute local paths and credentials from child output before it is stored."""
    text = text if isinstance(text, str) else ""
    text = _ANSI.sub("", text)
    text = _USERINFO.sub(lambda m: m.group(1) + "<redacted>@", text)
    text = _SECRET_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} <redacted>", text)
    text = _SECRET_PARAM.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    for known in sorted({str(x) for x in (*extra, os.path.expanduser("~"), os.getcwd()) if x}, key=len,
                        reverse=True):
        if len(known) > 3:
            for form in {known, known.replace("\\", "/"), known.replace("/", "\\")}:
                text = re.sub(re.escape(form) + r"[^\r\n'\"<>|]*", "<path>", text, flags=re.I)
    return _ABS_PATH.sub("<path>", text)


def _kill_tree(proc) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        proc.kill()
    except (OSError, subprocess.SubprocessError):
        pass


def _oversized(cwd: Path) -> bool:
    try:
        entries = list(os.scandir(cwd))
    except OSError:
        return False
    for entry in entries:
        try:
            # os.stat, not DirEntry.stat: on Windows the directory entry's size is stale while a file is written
            if entry.is_file(follow_symlinks=False) and os.stat(entry.path).st_size > MAX_OUTPUT_BYTES:
                return True
        except OSError:
            continue
    return False


def make_runner(total: float = TOTAL_TIMEOUT, per_call: float = CALL_TIMEOUT):
    """runner(argv, env, cwd) -> (rc, stdout, stderr) with a per-call and a total time budget. argv[0] must be
    an absolute path (no PATH or cwd lookup, shell=False). While the process runs, any file in cwd (including
    the captured stdout/stderr) that grows beyond MAX_OUTPUT_BYTES gets the process tree killed (rc 125)."""
    start = time.monotonic()
    counter = [0]

    def runner(argv, env, cwd):
        if not argv or not os.path.isabs(str(argv[0])):
            return 127, "", "refusing to run a command that is not an absolute path"
        remaining = total - (time.monotonic() - start)
        if remaining <= 0:
            return 124, "", "total time budget exhausted"
        limit = min(per_call, remaining)
        counter[0] += 1
        out_p, err_p = Path(cwd) / f".stdout{counter[0]}", Path(cwd) / f".stderr{counter[0]}"
        note = ""
        with open(out_p, "wb") as fo, open(err_p, "wb") as fe:
            try:
                proc = subprocess.Popen([str(a) for a in argv], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                        stdout=fo, stderr=fe, shell=False)
            except OSError as exc:
                return 127, "", f"cannot start the command: {type(exc).__name__}"
            deadline = time.monotonic() + limit
            rc = None
            while rc is None:
                try:
                    rc = proc.wait(timeout=min(0.1, max(deadline - time.monotonic(), 0.001)))
                except subprocess.TimeoutExpired:
                    if _oversized(Path(cwd)):
                        rc, note = 125, "\noutput exceeded the size limit; the process was stopped"
                    elif time.monotonic() >= deadline:
                        rc, note = 124, f"\ntimeout after {limit:.0f}s"
                    else:
                        continue
                    _kill_tree(proc)
                    proc.wait()

        def tail(p):
            with open(p, "rb") as fh:
                return fh.read(MAX_OUTPUT_BYTES).decode("utf-8", "replace")
        return rc, tail(out_p), tail(err_p) + note

    return runner


def expected_posts(wfp: str, n_files: int) -> int:
    """How many POST requests the client makes: about one per POST_FILES files or POST_BYTES bytes."""
    return max(1, math.ceil(max(len(wfp) / POST_BYTES, n_files / POST_FILES)))


def scan_timeout(posts: int) -> float:
    return min(SCAN_MAX, SCAN_BASE + SECONDS_PER_POST * posts / UPLOAD_THREADS * SAFETY)


def _client_version(cli: str, runner):
    tmp = Path(tempfile.mkdtemp(prefix="scanoss-ver-"))
    try:
        rc, so, se = runner([cli, "--version"], child_env(), str(tmp))
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    m = re.search(r"\b([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})\b", ((so or "") + " " + (se or ""))[:500], re.A)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if rc == 0 and m else None


def _clean(value, limit=MAX_STRING) -> str:
    return _CTRL.sub("", _ANSI.sub("", value))[:limit]


# ---- staging --------------------------------------------------------------------------------------

class _Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _displayable(rel: str) -> bool:
    return not (re.search(r"[\x00-\x1f\x7f]", rel) or approval._has_absolute_path("\n" + rel))


def _bad_rel(rel: str) -> bool:
    """Names that cannot go in a plan, or that could collide with the neutral names in the WFP body."""
    return not _displayable(rel) or (len(rel) >= 6 and bool(_NEUTRAL.fullmatch(rel)))


def _checked_bytes(path: Path, root: Path | None = None, rel: str | None = None) -> bytes:
    """The file's bytes, or _Reject(reason) when it must never be fingerprinted."""
    path = Path(path)
    try:
        if path.is_symlink():
            raise _Reject("symlink")
        if root is not None and rel is not None:
            real = os.path.normcase(os.path.realpath(path))
            want = os.path.normcase(os.path.join(os.path.realpath(root), *rel.split("/")))
            if real != want:
                raise _Reject("symlink")
        st = path.stat()
        if st.st_nlink > 1:                  # a hard link may alias a file outside the scan root
            raise _Reject("hardlink")
        if st.st_size > MAX_FILE_BYTES:
            raise _Reject("too_large")
        data = path.read_bytes()
    except OSError:
        raise _Reject("unreadable") from None
    if len(data) > MAX_FILE_BYTES:
        raise _Reject("too_large")
    if b"\0" in data[:8192]:
        raise _Reject("binary")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _Reject("non_utf8") from None
    return data


def stage(files: list[SourceFile], dest: Path) -> dict[str, str]:
    """Copy files to dest/1.ext, dest/2.ext, ... (extension kept); returns neutral name -> rel."""
    dest = Path(dest)
    mapping: dict[str, str] = {}
    for n, sf in enumerate(files, 1):
        ext = Path(sf.rel).suffix.lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9]{1,16}", ext):
            raise ValueError("bad_extension")
        try:
            data = _checked_bytes(sf.path)
        except _Reject as exc:
            raise ValueError(exc.reason) from None
        name = f"{n}.{ext}"
        (dest / name).write_bytes(data)
        mapping[name] = sf.rel
    return mapping


# ---- plan -----------------------------------------------------------------------------------------

def _prefixes(inv, includes: list[str]) -> list[str]:
    out = []
    for inc in includes:
        target = Path(os.path.normpath(inv.root / inc.replace("\\", "/")))
        rel = target.relative_to(inv.root).as_posix()
        out.append("" if rel == "." else rel.rstrip("/"))
    return out


def _under(rel: str, prefixes: list[str]) -> bool:
    return any(p == "" or rel == p or rel.startswith(p + "/") for p in prefixes)


def _find_colliders(body: str, id_map: dict) -> list[str]:
    bad = []
    for name, rel in id_map.items():
        mini = {"local_only": {"id_map": {name: rel}}, "followups": [],
                "requests": [{"url": ENDPOINT, "body": body}]}
        try:
            approval._check_containment(mini)
        except ApprovalError:
            bad.append(name)
    return bad


def _fingerprint(files: list[SourceFile], runner, cli: str):
    """Stage, fingerprint offline, sanitise. Returns (body, id_map {name: rel}). Temp dir always removed."""
    tmp = Path(tempfile.mkdtemp(prefix="scanoss-plan-"))
    try:
        stage_dir = tmp / "stage"
        stage_dir.mkdir()
        try:
            id_map = stage(files, stage_dir)
        except ValueError as exc:
            raise ScanossError("fingerprint_failed", f"a file changed while staging ({exc})") from None
        out = tmp / "files.wfp"
        try:
            rc, _so, err = runner([cli, "fingerprint", str(stage_dir), "-o", str(out), "--skip-settings-file"],
                                  child_env(), str(tmp))
        except Exception as exc:                                  # runner failures never leak paths
            raise ScanossError("fingerprint_failed", type(exc).__name__) from None
        if rc != 0 or not out.is_file():
            raise ScanossError("fingerprint_failed", scrub(err, (tmp,))[:300] or f"exit code {rc}")
        if out.stat().st_size > MAX_WFP_BYTES:
            raise ScanossError("plan_too_large", "the fingerprint file is larger than 8 MB; choose fewer files")
        try:
            body = sanitise_wfp(out.read_bytes().decode("utf-8"))
            described = describe_wfp(body)
        except UnicodeDecodeError:
            raise ScanossError("unsanitised_wfp", "the fingerprint file is not UTF-8") from None
        except ValueError as exc:
            code = str(exc) if str(exc) == "plan_too_large" else "unsanitised_wfp"
            raise ScanossError(code, "the fingerprint tool wrote something that is not a plain WFP; "
                                     "nothing was planned") from None
        names = [d["id"] for d in described]
        if not names:
            raise ScanossError("nothing_to_fingerprint", "the fingerprint tool produced no file entries")
        posts = expected_posts(body, len(names))
        if posts > MAX_EXPECTED_POSTS:
            raise ScanossError("plan_too_large", f"the upload would need about {posts} requests (limit "
                                                 f"{MAX_EXPECTED_POSTS}); reduce --include or --max-files")
        if any(n not in id_map for n in names):
            raise ScanossError("unsanitised_wfp", "the WFP names a file that was not staged")
        return body, {n: id_map[n] for n in names}, {n: rel for n, rel in id_map.items() if n not in names}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_plan(root: Path, includes: list[str], runner=None, max_files: int = SCANOSS_MAX_FILES,
              allow_untested: bool = False) -> dict:
    if runner is None:
        cli = find_cli(root)
        if cli is None:
            raise ScanossError("scanoss_not_installed", _NOT_INSTALLED)
        runner = make_runner()
    else:
        cli = find_cli(root) or "scanoss-py"
    if not includes:
        raise ScanossError("include_required", "choose the files to fingerprint with --include <path> [...]")
    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
        raise ScanossError("max_files_invalid", "--max-files must be a positive integer")
    if max_files > HARD_MAX_FILES:
        raise ScanossError("max_files_too_large", f"--max-files cannot exceed {HARD_MAX_FILES}")
    version = _client_version(cli, runner)
    tested = version is not None and version[:2] == TESTED_CLIENT
    if not tested and not allow_untested:
        seen = ".".join(map(str, version)) if version else "unknown"
        raise ScanossError("scanoss_version_untested",
                           f"scanoss-py {seen} is outside the tested range {TESTED_CLIENT[0]}.{TESTED_CLIENT[1]}.x "
                           "(the review of what it uploads was done for that range); install a tested version "
                           "(pip install 'scanoss>=1.54,<1.55') or pass --allow-untested-client")
    inv = inventory(Path(root))
    chosen = resolve_includes(inv, list(includes))
    prefixes = _prefixes(inv, includes)
    base_excluded = [(s.rel, s.reason) for s in inv.skipped if _under(s.rel, prefixes)]
    blocked: dict[str, str] = {}
    for _attempt in range(3):
        excluded = list(base_excluded) + [(rel, why) for rel, why in blocked.items()]
        eligible: list[SourceFile] = []
        for sf in chosen:
            if sf.rel in blocked:
                continue
            if len(eligible) >= max_files:
                excluded.append((sf.rel, "cap"))
                continue
            if _bad_rel(sf.rel):
                excluded.append((sf.rel, "bad_name"))
                continue
            try:
                _checked_bytes(sf.path, inv.root, sf.rel)
            except _Reject as exc:
                excluded.append((sf.rel, exc.reason))
                continue
            eligible.append(sf)
        if not eligible:
            raise InventoryError("no_source_files", "no file under the --include paths can be fingerprinted")
        body, id_map, dropped = _fingerprint(eligible, runner, cli)
        for name, rel in dropped.items():
            excluded.append((rel, "not_fingerprinted"))
        counts = {}
        for name, rel in id_map.items():
            sf = next(s for s in eligible if s.rel == rel)
            counts[name] = len(read_lines(sf.path))
        plan = _assemble(inv, body, id_map, counts, excluded, version, tested)
        if isinstance(plan, dict):
            return plan
        bad = _find_colliders(body, id_map)                       # plan is the ApprovalError
        if not bad:
            raise plan
        for name in bad:
            blocked[id_map[name]] = "name_collision"
    raise ScanossError("plan_invalid", "file names collide with the request text")


def _assemble(inv, body: str, id_map: dict, line_counts: dict, excluded: list, version=None, tested=True):
    reasons = Counter(why for _rel, why in excluded)
    listed = [{"rel": rel if _displayable(rel) else "(bad name)", "reason": why}
              for rel, why in sorted(excluded)][:MAX_EXCLUDED_LISTED]
    if len(excluded) > MAX_EXCLUDED_LISTED:
        listed.append({"rel": f"({len(excluded) - MAX_EXCLUDED_LISTED} more files)", "reason": "truncated_list"})
    n = len(id_map)
    posts = expected_posts(body, n)
    seen = ".".join(map(str, version)) if version else "unknown"
    disclosure = DISCLOSURE + [
        f"In this plan N is {n} file{'s' if n != 1 else ''}, {posts} expected upload request"
        f"{'s' if posts != 1 else ''} ({n} of {FREE_TIER_DAILY} files/day).",
        f"Client: scanoss-py {seen}. What the client uploads was analysed for scanoss-py "
        f"{TESTED_CLIENT[0]}.{TESTED_CLIENT[1]}.x."
        + ("" if tested else " This version is outside that range and was accepted with --allow-untested-client; "
                             "check the request bodies shown above."),
    ]
    request = {"id": "scanoss-1", "method": "POST", "url": ENDPOINT, "body": body, "source": None,
               "purpose": f"fingerprint lookup for {n} file{'s' if n != 1 else ''} under neutral names "
                          "(hashes and line numbers only)"}
    try:
        return build_plan(
            backend="scanoss", tier="fingerprint", root=str(inv.root), requests=[request], followups=[],
            disclosure=disclosure,
            cost={"requests": 1, "files": n, "quota_files": f"{n} of {FREE_TIER_DAILY} files/day",
                  "expected_upload_requests": posts, "client_version": seen,
                  "excluded_by_reason": dict(sorted(reasons.items()))},
            excluded=listed, local_only={"id_map": dict(id_map), "line_counts": dict(line_counts)})
    except ApprovalError as exc:
        if exc.code != "plan_invalid":
            raise
        return exc


# ---- results --------------------------------------------------------------------------------------

_LICENSE_EVIDENCE = {"component_declared": "scanoss_component_declared", "license_file": "scanoss_license_file"}
_PURL_SO = re.compile(r"pkg:stackoverflow/([0-9]{1,12})")
_RANGE = re.compile(r"\s*(\d{1,9})(?:\s*-\s*(\d{1,9}))?\s*")
_PCT = re.compile(r"\s*([0-9]{1,3}(?:\.[0-9]{1,6})?)\s*%\s*", re.ASCII)
_REPO_NOTE = ("SCANOSS matched this repository's copy of the code. The repository may itself contain a copy "
              "taken from elsewhere (for example a Stack Overflow answer), so the origin is not established. "
              "The open tier (Stack Exchange search) can check for a Stack Overflow source.")


def _s(value, limit=200):
    """A short, single-line, control-character-free copy of a string from the service (or None)."""
    if not isinstance(value, str):
        return None
    out = _clean(value[:20_000], min(limit, MAX_STRING)).strip()
    return out or None


def _url(value):
    if isinstance(value, str) and _CTRL.search(value):
        return None
    v = _s(value, 500)
    return v if v and re.fullmatch(r"https?://[^\s\x00-\x1f\x7f]+", v) else None


def _licenses(entry) -> list:
    out, seen = [], set()
    raw = entry.get("licenses")
    for lic in (raw if isinstance(raw, list) else [])[:20]:
        name = _s(lic.get("name"), 100) if isinstance(lic, dict) else None
        if not name:
            continue
        evidence = _LICENSE_EVIDENCE.get(lic.get("source"), "scanoss_other")
        if (name, evidence) in seen:
            continue
        seen.add((name, evidence))
        out.append({"spdx": name, "status": "declared", "evidence": evidence,
                    "confidence": "medium" if evidence != "scanoss_other" else "low"})
    return out or [{"spdx": None, "status": "none_found", "evidence": "scanoss_other", "confidence": "low"}]


def _candidate(entry):
    purls = [p[:200] for p in (entry.get("purl") if isinstance(entry.get("purl"), list) else [])[:10]
             if isinstance(p, str)]
    post = next((m for m in map(_PURL_SO.fullmatch, purls) if m), None)
    pm = _PCT.fullmatch(str(entry.get("matched", "")))
    common = dict(backend="scanoss", author=_s(entry.get("vendor"), 120), date=_s(entry.get("release_date"), 20),
                  revision=_s(entry.get("version"), 60), licenses=_licenses(entry),
                  matched_pct=float(pm.group(1)) if pm else None, oss_lines=_s(entry.get("oss_lines"), 60),
                  purl=purls)
    if post:
        pid = int(post.group(1))
        return Candidate(origin_url=f"https://stackoverflow.com/a/{pid}", kind="stackoverflow", post_id=pid,
                         note="SCANOSS knows a snapshot revision of this post; the live post may differ and "
                              "its licence is per revision.", **common)
    url = _url(entry.get("url"))
    if not url:
        return None
    return Candidate(origin_url=url, kind="repository", note=_REPO_NOTE, **common)


def _ranges(lines, count):
    """[(start, end)] for a SCANOSS lines value, clamped to the file; None when unusable."""
    whole = (1, max(count or 1, 1))
    if lines is None or str(lines).strip().lower() == "all":
        return [whole]
    out = []
    for part in str(lines).split(",")[:MAX_RANGES]:
        m = _RANGE.fullmatch(part)
        if not m:
            return None
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a < 1 or b < a or (count and a > count):
            return None
        out.append((a, min(b, count) if count else b))
    return out or None


def parse_scan(raw: dict, id_map: dict, line_counts: dict):
    """(matches, dropped, kb_version). Candidates only: several entries per file give several matches."""
    matches: list[RawMatch] = []
    dropped: list[dict] = []
    kb = None
    unknown = 0
    if not isinstance(raw, dict):
        return matches, dropped, kb
    for name, entries in raw.items():
        rel = id_map.get(name) if isinstance(name, str) else None
        if rel is None:
            unknown += 1
            if unknown <= 20:
                dropped.append({"rel": "(unknown file)", "reason": "unknown_file"})
            continue
        if not isinstance(entries, list):
            dropped.append({"rel": rel, "reason": "bad_entry"})
            continue
        count = line_counts.get(name) if isinstance(line_counts.get(name), int) else None
        real = [e for e in entries if isinstance(e, dict)]
        for e in real:
            sv = e.get("server")
            daily = sv.get("kb_version", {}).get("daily") if isinstance(sv, dict) and isinstance(
                sv.get("kb_version"), dict) else None
            kb = kb or _s(daily, 30)
        live = [e for e in real if e.get("id") != "none"]               # filtered BEFORE triage
        if declared_tree_reason(rel):
            dropped.extend({"rel": rel, "reason": "declared_dependency"} for _ in live[:1])
            continue
        for idx, e in enumerate(live):
            base = {"rel": rel, "id": _s(e.get("id"), 20), "matched": _s(e.get("matched"), 20),
                    "lines": _s(e.get("lines"), 60), "url": _url(e.get("url"))}
            if idx >= MAX_ENTRIES_PER_FILE:
                dropped.append({**base, "reason": "too_many"})
                continue
            if e.get("id") not in ("file", "snippet"):
                dropped.append({**base, "reason": "unknown_id"})
                continue
            mv = e.get("matched")
            pm = _PCT.fullmatch(mv) if isinstance(mv, str) else None
            if (pm is None and not (mv is None and e.get("id") == "file")) or (pm and float(pm.group(1)) > 100):
                dropped.append({**base, "reason": "bad_matched"})
                continue
            reason = scanoss_drop_reason(e)
            is_file = e.get("id") == "file" or str(e.get("lines", "")).strip().lower() == "all"
            spans = _ranges(e.get("lines"), count)
            if reason is None and spans is None:
                reason = "bad_lines"
            if reason is None and not is_file and sum(b - a + 1 for a, b in spans) < SCANOSS_MIN_LINES:
                reason = "below_threshold"
            cand = None if reason else _candidate(e)
            if reason is None and cand is None:
                reason = "no_origin_url"
            if reason:
                dropped.append({**base, "reason": reason})
                continue
            for a, b in spans:
                matches.append(RawMatch(rel=rel, start=a, end=b, match_type="file" if is_file else "near",
                                        candidate=cand))
    return matches, dropped, kb


# ---- run ---------------------------------------------------------------------------------------------

def _error(code, detail="", request_id=None):
    err = {"code": code, "detail": detail}
    if request_id:
        err["request_id"] = request_id
    return err


def run(plan_path: Path, approved_sha256: str, runner=None) -> dict:
    plan = load_plan(plan_path, approved_sha256)            # raises before anything is sent
    guard = Guard(plan)
    res = new_results("scanoss", "fingerprint", plan["sha256"], plan["root"])
    local = plan["local_only"]
    id_map = local.get("id_map") if isinstance(local.get("id_map"), dict) else {}
    counts = local.get("line_counts") if isinstance(local.get("line_counts"), dict) else {}
    reqs = plan["requests"]
    if len(reqs) > 1 or plan["followups"] or any(
            r["method"] != "POST" or r["url"] != ENDPOINT or r.get("headers") for r in reqs):
        raise ScanossError("plan_invalid", "not a SCANOSS fingerprint plan")
    injected = runner is not None
    cli = find_cli()
    if cli is None:
        if not injected:
            raise ScanossError("scanoss_not_installed", _NOT_INSTALLED)
        cli = "scanoss-py"
    for req in reqs:
        body = req["body"]
        try:
            if body is None or sanitise_wfp(body) != body:
                raise ValueError("unsanitised_wfp")
            describe_wfp(body)
        except ValueError:
            raise ScanossError("unsanitised_wfp", "the approved body is not a sanitised WFP; nothing was sent") \
                from None
        data = body.encode("utf-8")
        posts = expected_posts(body, len(id_map))
        if posts > MAX_EXPECTED_POSTS:
            raise ScanossError("plan_too_large", f"the upload would need about {posts} requests (limit "
                                                 f"{MAX_EXPECTED_POSTS}); nothing was sent")
        if not injected:
            budget = scan_timeout(posts)
            runner = make_runner(total=budget, per_call=budget)
        tmp = Path(tempfile.mkdtemp(prefix="scanoss-run-"))
        try:
            wfp = tmp / "approved.wfp"
            wfp.write_bytes(data)
            if wfp.read_bytes() != data:
                raise ScanossError("approved_body_mismatch", "the file to upload differs from the approved body")
            guard.check("POST", ENDPOINT, data)
            out = tmp / "result.json"
            argv = [cli, "scan", "--wfp", str(wfp), "--apiurl", API_BASE, "--skip-settings-file", "-o", str(out),
                    "--retry", "0", "--timeout", str(CALL_TIMEOUT - 5)]
            files = len(id_map)
            code, detail, status, started = None, "", None, True
            try:
                rc, _so, err = runner(argv, child_env(), str(tmp))
            except Exception as exc:
                rc, err = 1, f"{type(exc).__name__}"
            if rc != 0:
                # once the child has started, the service may already hold (part of) the upload
                started = not (rc == 127 and re.match(r"(cannot start|refusing)", err or ""))
                status = "possibly_sent" if started else None
                code = ("quota_exhausted" if re.search(r"\b429\b|too many requests", err or "", re.I)
                        else "backend_failed")
                detail = scrub(err, (tmp,)).strip()[:300] or f"scanoss-py exited with code {rc}"
            else:
                try:
                    if not out.is_file() or out.stat().st_size > MAX_OUTPUT_BYTES:
                        raise ValueError("missing or oversized output")
                    raw = json.loads(out.read_bytes().decode("utf-8-sig"))
                    if not isinstance(raw, dict):
                        raise ValueError("not an object")
                except (ValueError, RecursionError, OSError):
                    code, detail = "bad_response", "the scan output is missing, too large or not a JSON object"
                else:
                    status = 200
            guard.record("POST", ENDPOINT, data, status=status, attempts=1, error=code)
            if code:
                res["errors"].append(_error(code, detail, req["id"]))
                if not started:
                    res["not_sent"].append({"id": req["id"], "method": "POST", "url": ENDPOINT, "files": files,
                                            "reason": code})
            else:
                matches, dropped, kb = parse_scan(raw, id_map, {k: v for k, v in counts.items()
                                                                if isinstance(v, int)})
                res["matches"] = [raw_match_to_dict(m) for m in matches]
                res["dropped"] = dropped[:MAX_DROPPED]
                res["dropped_truncated"] = max(0, len(dropped) - MAX_DROPPED)
                res["kb_version"] = kb
                missing = [n for n in id_map if n not in raw]
                if missing:
                    res["notes"].append(f"no result came back for {len(missing)} fingerprinted file(s).")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    res["requests_sent"] = list(guard.sent)
    for entry in res["requests_sent"]:
        if entry.get("status") == "possibly_sent":
            entry["note"] = "upload may have reached the service"
    _notes(res, plan)
    _cap_results(res)
    return res


def _cap_results(res: dict) -> None:
    """Keep the results file under MAX_RESULTS_BYTES by halving matches and dropped, with a coverage error."""
    def size():
        return len(json.dumps(res, ensure_ascii=False))

    if size() <= MAX_RESULTS_BYTES:
        return
    before = len(res["matches"])
    while size() > MAX_RESULTS_BYTES and (res["matches"] or res["dropped"]):
        res["matches"] = res["matches"][:len(res["matches"]) // 2]
        res["dropped"] = res["dropped"][:len(res["dropped"]) // 2]
    res["errors"].append(_error("results_truncated", f"results exceeded {MAX_RESULTS_BYTES} bytes; kept "
                                                     f"{len(res['matches'])} of {before} matches"))


def _notes(res: dict, plan: dict) -> None:
    n = len(plan["local_only"].get("id_map", {})) if isinstance(plan["local_only"].get("id_map"), dict) else 0
    notes = res["notes"]
    if not plan["requests"]:
        notes.append("nothing_to_look_up: the plan has no requests, so nothing was fingerprinted or looked up.")
    notes.append(f"Fingerprint tier (SCANOSS free service): {n} file(s) you chose were looked up by hash. "
                 "Files outside your --include paths, and sensitive, vendored, generated, minified, oversized, "
                 "binary, non-UTF-8 or symlinked files, were never fingerprinted.")
    notes.append("Results are candidates, never a single established origin; a repository match may hold a copy "
                 "of code taken from elsewhere.")
    notes.append("Snippet matches under 50% matched or under 8 lines are dropped (below_threshold); whole-file "
                 "matches always count.")
    notes.append("No match means no match in the SCANOSS knowledge base" +
                 (f" (kb_version {res['kb_version']})" if res.get("kb_version") else "") +
                 "; it does not show that the code is original.")
    if res["errors"]:
        notes.append("The lookup failed: the fingerprinted files were NOT checked. See coverage errors.")
    if any(e.get("status") == "possibly_sent" for e in res["requests_sent"]):
        notes.append("The upload may have reached the service (and used free-tier quota) although the scan failed.")
    by_reason = plan["cost"].get("excluded_by_reason") if isinstance(plan["cost"], dict) else None
    if isinstance(by_reason, dict) and by_reason:
        res["excluded_by_reason"] = dict(by_reason)
        notes.append("excluded by reason: " + ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
    for e in plan["excluded"][:100]:
        notes.append(f"not fingerprinted: {e.get('rel')}: {e.get('reason')}")


# ---- CLI ---------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": "usage", "detail": message}))
        raise SystemExit(2)


def _numeric_key(name: str):
    m = re.match(r"(\d+)", name)
    return (int(m.group(1)) if m else 0, name)


def main(argv: list, runner=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = _Parser(prog="scanoss_backend.py")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    p = sub.add_parser("plan")
    p.add_argument("root")
    p.add_argument("--include", nargs="+", action="extend", default=[])
    p.add_argument("--out", required=True)
    p.add_argument("--max-files", type=int, default=SCANOSS_MAX_FILES)
    p.add_argument("--allow-untested-client", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("plan")
    r.add_argument("--sha256", required=True)
    r.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            plan = make_plan(Path(args.root), args.include, runner=runner, max_files=args.max_files,
                             allow_untested=args.allow_untested_client)
            write_plan(plan, Path(args.out), exclusive=True)
            id_map = plan["local_only"]["id_map"]
            files = [{"name": n, "rel": id_map[n]} for n in sorted(id_map, key=_numeric_key)]
            listing = ["", "Files to be fingerprinted (name sent -> your file):"] + [
                f"  {f['name']} -> {f['rel']}" for f in files[:500]]
            if len(files) > 500:
                listing.append(f"  ... and {len(files) - 500} more")
            print(json.dumps({"plan_path": str(Path(args.out).resolve()), "sha256": plan["sha256"],
                              "preview": render_preview(plan) + "\n".join(listing) + "\n",
                              "files": files, "excluded": plan["excluded"]}, ensure_ascii=False))
            return 0
        try:
            res = run(Path(args.plan), args.sha256, runner=runner)
        except (ApprovalError, ScanossError) as exc:
            res = new_results("scanoss", "fingerprint", args.sha256, "")
            res["errors"].append(_error(exc.code, str(exc)))
            write_results(Path(args.out), res)
            print(json.dumps({"error": exc.code, "detail": str(exc)}))
            return 2
        json.dumps(res, allow_nan=False)
        write_results(Path(args.out), res)
        if res["errors"]:
            first = res["errors"][0]
            print(json.dumps({"error": first["code"], "detail": first["detail"], "errors": len(res["errors"]),
                              "not_sent": len(res["not_sent"]), "results_path": str(Path(args.out).resolve())}))
            return 2
        print(json.dumps({"results_path": str(Path(args.out).resolve()), "matches": len(res["matches"]),
                          "dropped": len(res["dropped"]), "kb_version": res["kb_version"],
                          "requests_sent": len(res["requests_sent"]), "notes": res["notes"]}))
        return 0
    except ScanossError as exc:
        detail = exc.args[0] if exc.args else exc.code
        print(json.dumps({"error": exc.code, "detail": detail}))
    except (ApprovalError, InventoryError) as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}))
    except (OSError, ValueError):
        print(json.dumps({"error": "output_error", "detail": "file system or encoding error"}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
