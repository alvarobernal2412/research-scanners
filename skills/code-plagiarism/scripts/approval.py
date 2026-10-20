"""Plan/run approval gate: the privacy boundary of the skill.

Nothing derived from the user's code may leave the machine unless it is listed in a plan the
user approved by its sha256. This module builds and checks plans; `net.send` consults a
`Guard` before any I/O. There is deliberately no flag, environment variable or mode that
turns the checks off.

Secrets: a plan never holds a secret value. Headers use {"secret_from_env": NAME}; a URL may
carry one `{secret:NAME}` token as the whole value of a query parameter. NAME must be in
SECRET_VARS. `net.send` substitutes the value at send time.

local_only (data that is never sent: id->path maps, block hashes): paths are relative to the
scan root. Entries shaped {"kind": "hash", ...} are exempt from the containment check below,
so a hash that legitimately also appears in a WFP does not make the plan unbuildable.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

PLAN_VERSION = 1
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_FOLLOWUP_IDS = 100
MAX_FOLLOWUP_REQUESTS = 100_000
MAX_ID = 2 ** 63
TIERS = ("strict", "fingerprint", "open")
METHODS = ("GET", "POST")
SECRET_VARS = frozenset({"STACKEXCHANGE_KEY"})

_TOP_KEYS = {"version", "created", "sha256", "backend", "tier", "root", "requests", "followups",
             "disclosure", "cost", "excluded", "local_only"}
_REQ_REQUIRED = {"id", "method", "url", "body", "purpose", "source"}
_REQ_KEYS = _REQ_REQUIRED | {"headers"}
_FU_REQUIRED = {"method", "url_template", "id_param", "max_ids", "max_requests"}
_FU_KEYS = _FU_REQUIRED | {"headers"}

# Printable ASCII only; no '#' (urllib drops fragments) and no backslash. Used with fullmatch.
_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[\x21\x22\x24-\x5b\x5d-\x7e]*)?")
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRETISH_NAME = re.compile(r"auth|cookie|key|token|secret|passw|credential|session", re.I)
_FORBIDDEN_HEADERS = {"user-agent", "host", "content-length", "transfer-encoding", "connection"}
_REL_ABS_RE = re.compile(r"^(?:[A-Za-z]:|[\\/])|(?<![A-Za-z])[A-Za-z]:[\\/]")
# Absolute local paths anywhere in the plan. A POSIX path only counts at the start of a string or
# after whitespace, a quote, '=', ',', '(' or '['; so URL paths after a host are not flagged.
# Absolute local paths anywhere in the plan (fail closed). A POSIX path only counts at the start of a
# string or after whitespace, a quote, '=', ',', '(' or '[', so URL paths after a host are not flagged.
# Scanned by candidate positions (not one big alternation) so multi-megabyte bodies stay fast.
_ABS_TRIGGER = re.compile(r"[:/\\~]")
_ABS_AT = re.compile(
    r"\\\\[^\s\\]+\\|~[\\/]|/(?:[A-Za-z]/|(?:home|Users|root|tmp|var|mnt|etc|opt|usr"
    r"|Volumes|srv|workspace|data|private)(?:/|\b))", re.I)
_ABS_CTX = frozenset("\"'=,([")
_ABS_LITERALS = ("file:/", "%userprofile%", "%homepath%", "%appdata%", "%homedrive%", "%localappdata%")
_HOME_VAR = re.compile(r"\$\{?home\b")
_CREATED_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_IDS_RE = re.compile(r"[0-9]+(?:;[0-9]+)*", re.ASCII)
_PLACEHOLDER = "{ids}"
TOKEN_RE = re.compile(r"\{secret:([A-Z_][A-Z0-9_]*)\}")
_PARAM_NAME_RE = re.compile(r"[A-Za-z0-9_]+")
_MIN_LEAK_LEN = 6
MAX_SCAN_BYTES = 16 * 1024 * 1024
SECRET_PARAMS = frozenset({"key", "api_key", "apikey", "access_key", "token", "secret", "password", "auth"})
_HEX_RE = re.compile(r"[0-9a-fA-F]{8,128}")
_MAX_FIND_NEEDLES = 30
_RUN_WINDOW = 8
_RUN_OP_BUDGET = 1_000_000
_MIN_BASENAME_LEN = 10
_TOKEN_SPLIT = re.compile(r"[^\s\"'`<>(){}\[\],;:=&?|*#!]+")
_DELIMS = "\"'`<>(){}[],;:=&?|*#!"
_TO_SPACE = str.maketrans({c: " " for c in _DELIMS})


def _tokens(text: str) -> set:
    """Same tokens as _TOKEN_SPLIT.findall, but at C speed (translate + split)."""
    return set(text.translate(_TO_SPACE).split())


class ApprovalError(Exception):
    """code in plan_not_found|plan_invalid|plan_hash_mismatch|request_not_in_plan|followup_not_allowed
    |absolute_path_in_plan|plan_too_large|output_exists|output_error"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _describe(method, url) -> str:
    """Safe text for an exception: never the query string, never control characters."""
    if type(method) is not str or type(url) is not str:
        return "malformed request"
    base = url.split("?", 1)[0][:100]
    return _CONTROL_RE.sub("?", f"{method[:10]} {base}")


# ---- hashing / files ---------------------------------------------------------

def plan_hash(plan: dict) -> str:
    body = {k: v for k, v in plan.items() if k != "sha256"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_text(plan: dict) -> str:
    return json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write_plan(plan: dict, path, exclusive: bool = False) -> None:
    if plan.get("sha256") != plan_hash(plan):
        raise ApprovalError("plan_hash_mismatch", "plan sha256 does not match its content")
    data = _canonical_text(plan).encode("utf-8")
    if exclusive:
        try:
            with open(path, "xb") as fh:
                fh.write(data)
        except FileExistsError:
            raise ApprovalError("output_exists", "output file already exists") from None
        except OSError:
            raise ApprovalError("output_error", "cannot create the output file") from None
    else:
        Path(path).write_bytes(data)


def _no_dup_keys(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ApprovalError("plan_invalid", f"duplicate JSON key {k!r}")
        seen[k] = v
    return seen


def _read(path, approved_sha256, need_approval: bool) -> dict:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise ApprovalError("plan_not_found", "plan file not found or unreadable") from None
    try:
        text = raw.decode("utf-8")
        plan = json.loads(text, object_pairs_hook=_no_dup_keys,
                          parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ApprovalError:
        raise
    except ValueError:
        raise ApprovalError("plan_invalid", "plan file is not valid UTF-8 JSON") from None
    if not isinstance(plan, dict) or not isinstance(plan.get("sha256"), str):
        raise ApprovalError("plan_invalid", "plan must be an object with a sha256")
    try:
        recomputed = plan_hash(plan)
    except (TypeError, ValueError):
        raise ApprovalError("plan_invalid", "plan is not serialisable") from None
    if plan["sha256"] != recomputed:
        raise ApprovalError("plan_hash_mismatch", "plan content does not match its stored sha256")
    if need_approval:
        approved = approved_sha256.strip().lower() if isinstance(approved_sha256, str) else ""
        if not approved or approved != recomputed:
            raise ApprovalError("plan_hash_mismatch", "plan sha256 differs from the approved sha256")
    if text != _canonical_text(plan):
        raise ApprovalError("plan_hash_mismatch", "plan file bytes differ from the canonical form")
    validate_plan(plan)
    created = _parse_created(plan["created"])
    if created - datetime.now(timezone.utc) > MAX_FUTURE_SKEW:
        raise ApprovalError("plan_invalid", "plan 'created' is in the future")
    return plan


def load_plan(path, approved_sha256) -> dict:
    """Load a plan for a run: the user's approved hash is mandatory and must match."""
    return _read(path, approved_sha256, True)


def read_plan(path) -> dict:
    """Integrity-checked read for preview/strike (no approval is implied or granted)."""
    return _read(path, None, False)


def _parse_created(value) -> datetime:
    if not isinstance(value, str) or not _CREATED_RE.fullmatch(value):
        raise ApprovalError("plan_invalid", "plan 'created' must be an ISO UTC timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ApprovalError("plan_invalid", "plan 'created' is not a valid timestamp") from None


# ---- validation --------------------------------------------------------------

def _bad(msg: str):
    raise ApprovalError("plan_invalid", msg)


def _check_url(url, what: str, template: bool = False) -> None:
    if type(url) is not str:
        _bad(f"{what}: url must be a string")
    probe = url
    if template:
        if url.count(_PLACEHOLDER) != 1:
            _bad(f"{what}: url_template needs exactly one {_PLACEHOLDER}")
        probe = probe.replace(_PLACEHOLDER, "0")
    if template:
        host_end = url.find("/", 8)
        if host_end < 0 or url.find(_PLACEHOLDER) < host_end:
            _bad(f"{what}: {_PLACEHOLDER} may only appear after the host")
    if "?" in probe:
        for pair in probe.split("?", 1)[1].split("&"):
            name, _, val = pair.partition("=")
            if (name.lower() in SECRET_PARAMS or "%" in name and unquote(name).lower() in SECRET_PARAMS) \
                    and not TOKEN_RE.fullmatch(val):
                _bad(f"{what}: secret-looking literal query parameter {name!r}; use the {{secret:NAME}} token")
    tokens = list(TOKEN_RE.finditer(probe)) if "{secret:" in probe else []
    if probe.count("{secret:") != len(tokens) or len(tokens) > 1:
        _bad(f"{what}: at most one well-formed {{secret:NAME}} token is allowed")
    if tokens:
        m = tokens[0]
        if m.group(1) not in SECRET_VARS:
            _bad(f"{what}: secret {m.group(1)!r} is not an allowed secret")
        i = m.start()
        q = probe.find("?")
        prefix = probe[:i - 1]
        k = max(prefix.rfind("?"), prefix.rfind("&"))
        if (q < 0 or probe[i - 1:i] != "=" or k < q or prefix[k + 1:] != "key"
                or probe[m.end():m.end() + 1] not in ("", "&")):
            _bad(f"{what}: the secret token must be the whole value of a query parameter")
        probe = probe.replace(m.group(0), "0")
    if not _URL_RE.fullmatch(probe):
        _bad(f"{what}: url must be https, printable ASCII, with no userinfo, fragment or backslash")


def _check_headers(headers, what: str) -> None:
    if not isinstance(headers, dict):
        _bad(f"{what}: headers must be an object")
    seen = set()
    for name, spec in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
            _bad(f"{what}: invalid header name")
        low = name.lower()
        if low in seen or low in _FORBIDDEN_HEADERS:
            _bad(f"{what}: header {name!r} is duplicated or reserved")
        seen.add(low)
        if isinstance(spec, dict):
            env = spec.get("secret_from_env")
            if set(spec) != {"secret_from_env"} or not isinstance(env, str) or not _ENV_NAME_RE.fullmatch(env):
                _bad(f"{what}: bad secret_from_env marker for {name!r}")
            if env not in SECRET_VARS:
                _bad(f"{what}: environment variable {env!r} is not an allowed secret")
        elif isinstance(spec, str):
            if _CONTROL_RE.search(spec) or TOKEN_RE.search(spec):
                _bad(f"{what}: control characters or a secret token in header {name!r}")
            if _SECRETISH_NAME.search(name):
                _bad(f"{what}: header {name!r} looks secret; use secret_from_env")
        else:
            _bad(f"{what}: header {name!r} must be a string or a secret_from_env marker")


def _walk_strings(obj, out: list) -> None:
    """Collect every string (keys and values) without per-element recursion."""
    stack = [obj]
    pop, push, add = stack.pop, stack.extend, out.append
    while stack:
        o = pop()
        t = type(o)
        if t is str:
            add(o)
        elif t is dict:
            push(o.keys())
            push(o.values())
        elif t is list or t is tuple:
            push(o)


def _has_absolute_path(joined: str) -> bool:
    low = joined.lower()
    if any(tok in low for tok in _ABS_LITERALS) or ("$home" in low or "${home" in low) and _HOME_VAR.search(low):
        return True
    for m in _ABS_TRIGGER.finditer(joined):
        i = m.start()
        if joined[i] == ":":
            nxt, prev = joined[i + 1:i + 2], joined[i - 1:i] if i else ""
            if (nxt in ("/", "\\") and nxt and prev.isascii() and prev.isalpha()
                    and not (i >= 2 and joined[i - 2].isascii() and joined[i - 2].isalnum())):
                return True
        elif (i == 0 or joined[i - 1].isspace() or joined[i - 1] in _ABS_CTX) and _ABS_AT.match(joined, i):
            return True
    return False


def validate_plan(plan: dict) -> None:
    """Structural validation plus an absolute-path scan of EVERY string in the plan."""
    if not isinstance(plan, dict):
        _bad("plan must be an object")
    extra = set(plan) - _TOP_KEYS
    if extra:
        _bad("unknown plan fields: " + ", ".join(sorted(map(str, extra))))
    missing = (_TOP_KEYS - {"sha256"}) - set(plan)
    if missing:
        _bad("missing plan fields: " + ", ".join(sorted(missing)))
    if type(plan["version"]) is not int or plan["version"] != PLAN_VERSION:
        _bad(f"unsupported plan version (expected {PLAN_VERSION})")
    _parse_created(plan["created"])
    if not isinstance(plan["backend"], str) or not plan["backend"]:
        _bad("backend must be a non-empty string")
    if plan["tier"] not in TIERS:
        _bad("tier must be one of " + "|".join(TIERS))
    if not isinstance(plan["root"], str) or _REL_ABS_RE.search(plan["root"]) \
            or ".." in plan["root"].replace("\\", "/").split("/"):
        _bad("root must be a bare directory name, not an absolute path")
    if not isinstance(plan["disclosure"], list) or not all(isinstance(s, str) for s in plan["disclosure"]):
        _bad("disclosure must be a list of strings")
    if not isinstance(plan["cost"], dict):
        _bad("cost must be an object")
    if not isinstance(plan["excluded"], list) or not all(isinstance(e, dict) for e in plan["excluded"]):
        _bad("excluded must be a list of objects")
    if not isinstance(plan["local_only"], dict):
        _bad("local_only must be an object")
    if not isinstance(plan["requests"], list) or not isinstance(plan["followups"], list):
        _bad("requests and followups must be lists")

    ids, keys = set(), set()
    for i, r in enumerate(plan["requests"]):
        what = f"requests[{i}]"
        if not isinstance(r, dict):
            _bad(f"{what} must be an object")
        if not _REQ_REQUIRED <= set(r) or set(r) - _REQ_KEYS:
            _bad(f"{what} has missing or unknown fields")
        if not isinstance(r["id"], str) or not r["id"] or r["id"] in ids or "," in r["id"]:
            _bad(f"{what}: id must be a unique non-empty string without commas")
        ids.add(r["id"])
        if r["method"] not in METHODS:
            _bad(f"{what}: method must be GET or POST")
        _check_url(r["url"], what)
        body = r["body"]
        if body is not None:
            if not isinstance(body, str):
                _bad(f"{what}: body must be a string or null")
            try:
                body.encode("utf-8")
            except UnicodeEncodeError:
                _bad(f"{what}: body is not valid unicode")
            if "{secret:" in body and TOKEN_RE.search(body):
                _bad(f"{what}: a secret token is not allowed in a body")
        if not isinstance(r["purpose"], str):
            _bad(f"{what}: purpose must be a string")
        src = r["source"]
        if src is not None:
            if (not isinstance(src, dict) or set(src) != {"rel", "start", "end"}
                    or not isinstance(src["rel"], str) or _REL_ABS_RE.search(src["rel"])
                    or not all(isinstance(src[k], int) and not isinstance(src[k], bool) for k in ("start", "end"))):
                _bad(f"{what}: source must be {{rel, start, end}} with a relative path")
        if "headers" in r:
            _check_headers(r["headers"], what)
        key = (r["method"], r["url"], body)
        if key in keys:
            _bad(f"{what}: duplicate request")
        keys.add(key)

    for i, f in enumerate(plan["followups"]):
        what = f"followups[{i}]"
        if not isinstance(f, dict) or not _FU_REQUIRED <= set(f) or set(f) - _FU_KEYS:
            _bad(f"{what} has missing or unknown fields")
        if f["method"] != "GET":
            _bad(f"{what}: follow-ups must be GET")
        _check_url(f["url_template"], what, template=True)
        if f["id_param"] != "ids":
            _bad(f"{what}: id_param must be 'ids'")
        for fld, hi in (("max_ids", MAX_FOLLOWUP_IDS), ("max_requests", MAX_FOLLOWUP_REQUESTS)):
            m = f[fld]
            if not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= hi:
                _bad(f"{what}: {fld} must be an integer 1..{hi}")
        if "headers" in f:
            _check_headers(f["headers"], what)

    strings: list = []
    _walk_strings(plan, strings)
    # one pass: a newline separator gives every string the same "start of string" context
    if _has_absolute_path("\n".join(strings)):
        raise ApprovalError("absolute_path_in_plan",
                            "the plan contains an absolute local path; use paths relative to the scan root")


# ---- containment (build time only) -----------------------------------------------

def _is_hash_entry(obj) -> bool:
    return (isinstance(obj, dict) and set(obj) == {"kind", "value"} and obj["kind"] == "hash"
            and isinstance(obj["value"], str) and bool(_HEX_RE.fullmatch(obj["value"])))


def _local_needles(obj, out: set) -> None:
    """Strings from local_only that must not appear in outgoing text. Only the exact shape
    {"kind": "hash", "value": <hex>} is exempt; every other string is scanned."""
    stack = [obj]
    while stack:
        o = stack.pop()
        t = type(o)
        if t is str:
            if len(o) >= _MIN_LEAK_LEN:
                out.add(o)
            if "/" in o or "\\" in o:
                base = o.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
                if len(base) >= _MIN_BASENAME_LEN:
                    out.add(base)
        elif t is dict:
            if _is_hash_entry(o):
                continue
            for k, v in o.items():
                if type(k) is str and ("/" in k or "\\" in k):
                    stack.append(k)
                stack.append(v)
        elif t is list or t is tuple:
            stack.extend(o)


def _norm(text: str) -> str:
    return text.casefold().replace("\\", "/")


def _outgoing_text(plan: dict) -> str:
    parts = []
    for r in plan["requests"]:
        parts.append(r["url"])
        if r["body"]:
            parts.append(r["body"])
        parts.extend(v for v in (r.get("headers") or {}).values() if isinstance(v, str))
    for f in plan["followups"]:
        parts.append(f["url_template"])
        parts.extend(v for v in (f.get("headers") or {}).values() if isinstance(v, str))
    blob = "\n".join(parts)
    if len(blob) > MAX_SCAN_BYTES:
        raise ApprovalError("plan_too_large", "outgoing text is too large to check for local data")
    return blob


def _check_containment(plan: dict) -> None:
    """Best-effort safety net, not a guarantee: no local_only string (nor a path-like key or the basename
    of a path) may appear in any outgoing URL, body or header value, also after percent-decoding.

    Near-linear. (1) The text is split into tokens and looked up in sets: exact needles by token or by a
    bounded window of '/'-components; needles containing delimiter characters by their rarest token
    (anchor), confirmed when the needle's interior tokens are present and a substring search succeeds.
    (2) A substring search for a capped number (_MAX_FIND_NEEDLES) of the longest needles catches
    occurrences glued to other characters. Beyond those caps only (1) applies."""
    raw: set = set()
    _local_needles(plan["local_only"], raw)
    if not raw:
        return
    exact, slow = set(), []
    for n in raw:
        n = _norm(n)
        if _TOKEN_SPLIT.fullmatch(n):
            exact.add(n)
        else:
            slow.append(n)
    run_count: dict = {}
    split = [(n, _TOKEN_SPLIT.findall(n)) for n in slow]
    for _n, runs in split:
        for r in set(runs):
            run_count[r] = run_count.get(r, 0) + 1
    anchored: dict = {}
    find_list = []
    for n, runs in split:
        cands = [r for r in runs if len(r) >= 3]
        if not cands:
            find_list.append(n)
            continue
        anchor = min(cands, key=lambda r: (run_count[r], -len(r)))
        interior = tuple(r for r in runs[1:-1] if r != anchor)
        anchored.setdefault(anchor, []).append((n, interior))
    ordered = sorted(exact, key=len, reverse=True)
    find_list += [n for n in ordered if "/" in n or "." in n or len(n) >= 12]
    find_list = find_list[:_MAX_FIND_NEEDLES]
    blob = _outgoing_text(plan)
    texts = [blob]
    if "%" in blob:
        decoded = unquote(blob)
        if len(decoded) > MAX_SCAN_BYTES:
            raise ApprovalError("plan_too_large", "outgoing text is too large to check for local data")
        texts.append(decoded)
    leak = "a local_only value appears in an outgoing request"
    ops = _RUN_OP_BUDGET
    for text in texts:
        text = _norm(text)
        tokens = _tokens(text)
        if not tokens.isdisjoint(exact):
            _bad(leak)
        for key in tokens.intersection(anchored):
            for n, interior in anchored[key]:
                if all(r in tokens for r in interior) and n in text:
                    _bad(leak)
        for tok in [t for t in tokens if "/" in t]:
            if ops > 0:
                comps = tok.split("/")
                k = len(comps)
                for i in range(k):
                    for j in range(i + 1, min(k, i + _RUN_WINDOW) + 1):
                        ops -= 1
                        run = "/".join(comps[i:j])
                        if len(run) >= _MIN_LEAK_LEN and run in exact:
                            _bad(leak)
                    if ops <= 0:
                        break
        for n in find_list:
            if n in text:
                _bad(leak)


# ---- build / strike ------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _root_name(root) -> str:
    text = str(root).replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1] if text else ""


def _copy(obj):
    """Deep copy of JSON-shaped data (much faster than copy.deepcopy)."""
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        raise ApprovalError("plan_invalid", "plan inputs must be JSON data") from None


def build_plan(backend: str, tier: str, root: str, requests: list, followups: list,
               disclosure: list, cost: dict, excluded: list, local_only: dict) -> dict:
    reqs = []
    for r in _copy(list(requests)):
        if isinstance(r, dict):
            r.setdefault("body", None)
            r.setdefault("purpose", "")
            r.setdefault("source", None)
        reqs.append(r)
    plan = {
        "version": PLAN_VERSION,
        "created": _now_iso(),
        "backend": backend,
        "tier": tier,
        "root": _root_name(root),
        "requests": reqs,
        "followups": _copy(list(followups)),
        "disclosure": list(disclosure),
        "cost": _copy(cost),
        "excluded": _copy(list(excluded)),
        "local_only": _copy(local_only),
    }
    validate_plan(plan)
    _check_containment(plan)
    plan["sha256"] = plan_hash(plan)
    return plan


def strike(plan: dict, request_ids: set) -> dict:
    """Return a new plan without the given requests (new created/sha256)."""
    wanted = set(request_ids)
    have = {r["id"] for r in plan["requests"]}
    unknown = wanted - have
    if unknown:
        raise ApprovalError("plan_invalid", "unknown request ids: " + ", ".join(sorted(unknown)))
    new = copy.deepcopy(plan)
    new["requests"] = [r for r in new["requests"] if r["id"] not in wanted]
    new["created"] = _now_iso()
    cost, n = new["cost"], len(new["requests"])
    if isinstance(cost.get("requests"), int):
        cost["requests"] = n
    if isinstance(cost.get("quota"), str):
        m = re.match(r"^\d+( of .*)$", cost["quota"])
        if m:
            cost["quota"] = f"{n}{m.group(1)}"
        else:
            cost["stale"] = True
    if set(cost) - {"requests", "quota", "stale"}:
        cost["stale"] = True
    validate_plan(new)
    new["sha256"] = plan_hash(new)
    return new


# ---- preview ---------------------------------------------------------------------

def _hosts(plan: dict) -> list:
    hosts = set()
    for u in [r["url"] for r in plan["requests"]] + [f["url_template"] for f in plan["followups"]]:
        hosts.add(u.split("/", 3)[2])
    return sorted(hosts)


def _show_url(url: str) -> str:
    return TOKEN_RE.sub(lambda m: f"<secret from {m.group(1)}; not shown>", url)


def _fmt_headers(headers: dict) -> str:
    parts = []
    for name, spec in headers.items():
        if isinstance(spec, dict):
            parts.append(f"{name}: <secret read from environment variable {spec['secret_from_env']}; value not shown>")
        else:
            parts.append(f"{name}: {spec}")
    return "; ".join(parts)


def render_preview(plan: dict) -> str:
    out = ["PLAN PREVIEW - nothing is sent until you approve this exact plan by its sha256.",
           f"Service: {plan['backend']}",
           f"Tier: {plan['tier']}",
           "Endpoint: " + ", ".join(_hosts(plan)),
           f"Created: {plan['created']}",
           f"sha256: {plan.get('sha256', plan_hash(plan))}",
           "", f"Requests ({len(plan['requests'])}), sent exactly as written:"]
    keyed = set()
    for r in plan["requests"]:
        keyed.update(m.group(1) for m in TOKEN_RE.finditer(r["url"]))
        out.append(f"[{r['id']}] {r['method']} {_show_url(r['url'])}")
        if r["purpose"]:
            out.append(f"    purpose: {r['purpose']}")
        if r["source"]:
            s = r["source"]
            out.append(f"    from your code: {s['rel']} lines {s['start']}-{s['end']}")
        if r.get("headers"):
            out.append("    headers: " + _fmt_headers(r["headers"]))
        if r["body"] is not None:
            out.append(f"    body ({len(r['body'].encode('utf-8'))} bytes):")
            out.append("<<<")
            out.append(r["body"])
            out.append(">>>")
    if plan["followups"]:
        out += ["", "Follow-up requests (only with public ids returned by the service):"]
        for f in plan["followups"]:
            keyed.update(m.group(1) for m in TOKEN_RE.finditer(f["url_template"]))
            out.append(f"  {f['method']} {_show_url(f['url_template'])}  "
                       f"(at most {f['max_ids']} ids per request, {f['max_requests']} requests)")
            if f.get("headers"):
                out.append("    headers: " + _fmt_headers(f["headers"]))
    out += ["", "Not sent: local file paths and names, file sizes, and any source text that is not "
                "shown in the requests above."]
    if plan["local_only"]:
        out.append(f"Kept on this machine only: {len(plan['local_only'])} local_only entries "
                   f"({', '.join(sorted(map(str, plan['local_only'])))}).")
    out += ["", "Network: requests may pass through your configured proxy (HTTPS_PROXY / system proxy); "
                "TLS stays end-to-end."]
    for name in sorted(keyed):
        out.append(f"Keyed requests: the value of environment variable {name} is added at send time "
                   "and is never written to the plan, this preview or the logs.")
    if plan["cost"]:
        out += ["", "Cost against quota:"]
        for k in sorted(plan["cost"]):
            out.append(f"  {k}: {json.dumps(plan['cost'][k], ensure_ascii=False)}")
    if plan["excluded"]:
        out += ["", f"Excluded ({len(plan['excluded'])}):"]
        for e in plan["excluded"]:
            if set(e) <= {"rel", "reason"} and "rel" in e:
                out.append(f"  {e['rel']}: {e.get('reason', '')}")
            else:
                out.append("  " + json.dumps(e, ensure_ascii=False, sort_keys=True))
    if plan["disclosure"]:
        out += ["", "Disclosure:"] + [f"  - {d}" for d in plan["disclosure"]]
    return "\n".join(out) + "\n"


# ---- guard -------------------------------------------------------------------------

class Guard:
    """Allows exactly the planned requests (and id-bearing follow-ups), each at most once per run.

    `check` authorises; `net.send` then calls `record` with the outcome (or `release` when it could
    not send), so `sent` lists only requests that were actually attempted."""

    def __init__(self, plan: dict):
        validate_plan(plan)
        if plan.get("sha256") != plan_hash(plan):
            raise ApprovalError("plan_hash_mismatch", "plan content does not match its sha256")
        self._index: dict = {}
        for r in plan["requests"]:
            body = None if r["body"] is None else r["body"].encode("utf-8")
            self._index[(r["method"], r["url"], body)] = r.get("headers") or {}
        self._followups = []
        for f in plan["followups"]:
            pre, suf = f["url_template"].split(_PLACEHOLDER)
            self._followups.append((f["method"], pre, suf, f["max_ids"], f["max_requests"],
                                    f.get("headers") or {}))
        self._fu_used = [0] * len(self._followups)
        self._allowed: set = set()
        self._authorised: dict = {}     # (method, url, body) -> followup index or None
        self.sent: list = []

    def allow_ids(self, ids: Iterable[int]) -> None:
        batch = []
        for i in ids:
            if type(i) is not int or not 1 <= i < MAX_ID:
                raise ApprovalError("followup_not_allowed", "ids must be integers in 1..2**63-1")
            batch.append(i)
        self._allowed.update(batch)

    def _match(self, method, url, body):
        """Return (header_spec, followup_index or None) or raise."""
        if (type(method) is not str or type(url) is not str
                or not (body is None or type(body) is bytes)
                or _CONTROL_RE.search(method) or _CONTROL_RE.search(url)):
            raise ApprovalError("request_not_in_plan", "malformed request")
        spec = self._index.get((method, url, body))
        if spec is not None:
            return spec, None
        error = None
        for idx, (fmethod, pre, suf, max_ids, _max_req, fheaders) in enumerate(self._followups):
            if method != fmethod or body is not None:
                continue
            if len(url) < len(pre) + len(suf) or not url.startswith(pre) or not url.endswith(suf):
                continue
            mid = url[len(pre):len(url) - len(suf)]
            tokens = mid.split(";")
            if not _IDS_RE.fullmatch(mid) or len(tokens) > max_ids or any(len(t) > 19 for t in tokens):
                error = error or ApprovalError("followup_not_allowed", "follow-up ids are malformed or too many")
                continue
            nums = [int(t) for t in tokens]
            if [str(n) for n in nums] != tokens or nums != sorted(set(nums)) or nums[0] < 1:
                error = error or ApprovalError("followup_not_allowed", "ids must be canonical, ascending, unique")
            elif not set(nums) <= self._allowed:
                error = error or ApprovalError("followup_not_allowed", "id was not returned by the service")
            else:
                return fheaders, idx
        if error:
            raise error
        raise ApprovalError("request_not_in_plan", f"{_describe(method, url)} is not in the approved plan")

    def planned_headers(self, method, url, body) -> dict:
        """Header spec ({name: literal | {"secret_from_env": VAR}}) of the matching plan entry."""
        return self._match(method, url, body)[0]

    def check(self, method: str, url: str, body, headers=None) -> None:
        spec, fidx = self._match(method, url, body)
        if headers:
            if not isinstance(headers, dict):
                raise ApprovalError("request_not_in_plan", "headers must be a mapping")
            planned = {k.lower(): v for k, v in spec.items()}
            seen = set()
            for name, value in headers.items():
                if (type(name) is not str or type(value) is not str
                        or not _HEADER_NAME_RE.fullmatch(name) or _CONTROL_RE.search(value)):
                    raise ApprovalError("request_not_in_plan", "malformed header")
                low = name.lower()
                want = planned.get(low)
                if low in seen or want is None or isinstance(want, dict) or want != value:
                    raise ApprovalError("request_not_in_plan", "header is not in the plan")
                seen.add(low)
        key = (method, url, body)
        if key in self._authorised:
            raise ApprovalError("request_not_in_plan", "this request was already made in this run")
        if fidx is not None:
            if self._fu_used[fidx] >= self._followups[fidx][4]:
                raise ApprovalError("followup_not_allowed", "follow-up request limit reached")
            self._fu_used[fidx] += 1
        self._authorised[key] = fidx

    def release(self, method, url, body) -> None:
        """Undo `check` for a request that was never sent (for example, a missing secret)."""
        fidx = self._authorised.pop((method, url, body), None)
        if fidx is not None:
            self._fu_used[fidx] -= 1

    def record(self, method, url, body, status=None, attempts=0, error=None) -> None:
        """Log an attempted request: method, URL (placeholders only) and body hash, never body text."""
        self.sent.append({
            "method": method, "url": url,
            "body_sha256": None if body is None else hashlib.sha256(body).hexdigest(),
            "body_bytes": 0 if body is None else len(body),
            "followup": self._authorised.get((method, url, body)) is not None,
            "status": status, "attempts": attempts, "error": error,
        })


# ---- CLI ----------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": "usage", "detail": message}))
        raise SystemExit(2)


def _emit(plan_path, plan) -> None:
    print(json.dumps({"plan_path": str(Path(plan_path).resolve()), "sha256": plan["sha256"],
                      "preview": render_preview(plan)}, ensure_ascii=False))


def main(argv: list) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = _Parser(prog="approval.py")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    p = sub.add_parser("preview")
    p.add_argument("plan")
    s = sub.add_parser("strike")
    s.add_argument("plan")
    s.add_argument("--ids", required=True)
    s.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        plan = read_plan(Path(args.plan))
        if args.cmd == "preview":
            _emit(args.plan, plan)
        else:
            new = strike(plan, {i for i in args.ids.split(",") if i})
            write_plan(new, Path(args.out), exclusive=True)
            _emit(args.out, new)
    except ApprovalError as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}))
        return 2
    except OSError:
        print(json.dumps({"error": "output_error", "detail": "file system error"}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
