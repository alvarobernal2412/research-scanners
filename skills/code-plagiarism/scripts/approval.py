"""Plan/run approval gate: the privacy boundary of the skill.

Nothing derived from the user's code may leave the machine unless it is listed in a plan the
user approved by its sha256. This module builds and checks plans; `net.send` consults a
`Guard` before any I/O. There is deliberately no flag, environment variable or mode that
turns the checks off.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

PLAN_VERSION = 1
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_FOLLOWUP_IDS = 100
TIERS = ("strict", "fingerprint", "open")
METHODS = ("GET", "POST")

_TOP_KEYS = {"version", "created", "sha256", "backend", "tier", "root", "requests", "followups",
             "disclosure", "cost", "excluded", "local_only"}
_REQ_REQUIRED = {"id", "method", "url", "body", "purpose", "source"}
_REQ_KEYS = _REQ_REQUIRED | {"headers"}
_FU_REQUIRED = {"method", "url_template", "id_param", "max_ids"}
_FU_KEYS = _FU_REQUIRED | {"headers"}

# Printable ASCII only; no '#' (urllib drops fragments) and no backslash.
_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[\x21\x22\x24-\x5b\x5d-\x7e]*)?$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRETISH_NAME = re.compile(r"auth|cookie|key|token|secret|passw|credential|session", re.I)
_FORBIDDEN_HEADERS = {"user-agent", "host", "content-length", "transfer-encoding", "connection"}
_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:|[\\/])|(?<![A-Za-z])[A-Za-z]:[\\/]")
_CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_IDS_RE = re.compile(r"[0-9]+(?:;[0-9]+)*", re.ASCII)
_PLACEHOLDER = "{ids}"
_MIN_LEAK_LEN = 6


class ApprovalError(Exception):
    """code in plan_not_found|plan_invalid|plan_hash_mismatch|request_not_in_plan|followup_not_allowed"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _short(text: str, n: int = 100) -> str:
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= n else text[:n] + "..."


# ---- hashing / files ---------------------------------------------------------

def plan_hash(plan: dict) -> str:
    body = {k: v for k, v in plan.items() if k != "sha256"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_text(plan: dict) -> str:
    return json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write_plan(plan: dict, path) -> None:
    if plan.get("sha256") != plan_hash(plan):
        raise ApprovalError("plan_hash_mismatch", "plan sha256 does not match its content")
    Path(path).write_bytes(_canonical_text(plan).encode("utf-8"))


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
    if not isinstance(value, str) or not _CREATED_RE.match(value):
        raise ApprovalError("plan_invalid", "plan 'created' must be an ISO UTC timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ApprovalError("plan_invalid", "plan 'created' is not a valid timestamp") from None


# ---- validation --------------------------------------------------------------

def _bad(msg: str):
    raise ApprovalError("plan_invalid", msg)


def _check_url(url, what: str, template: bool = False) -> None:
    if not isinstance(url, str):
        _bad(f"{what}: url must be a string")
    probe = url
    if template:
        if url.count(_PLACEHOLDER) != 1:
            _bad(f"{what}: url_template needs exactly one {_PLACEHOLDER}")
        probe = url.replace(_PLACEHOLDER, "0")
    if not _URL_RE.match(probe):
        _bad(f"{what}: url must be https, printable ASCII, with no userinfo, fragment or backslash")


def _check_headers(headers, what: str) -> None:
    if not isinstance(headers, dict):
        _bad(f"{what}: headers must be an object")
    seen = set()
    for name, spec in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME_RE.match(name):
            _bad(f"{what}: invalid header name")
        low = name.lower()
        if low in seen or low in _FORBIDDEN_HEADERS:
            _bad(f"{what}: header {name!r} is duplicated or reserved")
        seen.add(low)
        if isinstance(spec, dict):
            env = spec.get("secret_from_env")
            if set(spec) != {"secret_from_env"} or not isinstance(env, str) or not _ENV_NAME_RE.match(env):
                _bad(f"{what}: bad secret_from_env marker for {name!r}")
        elif isinstance(spec, str):
            if _CONTROL_RE.search(spec):
                _bad(f"{what}: control characters in header {name!r}")
            if _SECRETISH_NAME.search(name):
                _bad(f"{what}: header {name!r} looks secret; use secret_from_env")
        else:
            _bad(f"{what}: header {name!r} must be a string or a secret_from_env marker")


def _walk_strings(obj, out: list) -> None:
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk_strings(k, out)
            _walk_strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_strings(v, out)


def validate_plan(plan: dict) -> None:
    """Structural validation. Raises ApprovalError('plan_invalid') on anything unexpected."""
    if not isinstance(plan, dict):
        _bad("plan must be an object")
    extra = set(plan) - _TOP_KEYS
    if extra:
        _bad("unknown plan fields: " + ", ".join(sorted(map(str, extra))))
    missing = (_TOP_KEYS - {"sha256"}) - set(plan)
    if missing:
        _bad("missing plan fields: " + ", ".join(sorted(missing)))
    if plan["version"] != PLAN_VERSION or isinstance(plan["version"], bool):
        _bad(f"unsupported plan version (expected {PLAN_VERSION})")
    _parse_created(plan["created"])
    if not isinstance(plan["backend"], str) or not plan["backend"]:
        _bad("backend must be a non-empty string")
    if plan["tier"] not in TIERS:
        _bad("tier must be one of " + "|".join(TIERS))
    if not isinstance(plan["root"], str) or _ABS_PATH_RE.search(plan["root"]):
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
        if not isinstance(r["id"], str) or not r["id"] or r["id"] in ids:
            _bad(f"{what}: id must be a unique non-empty string")
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
        if not isinstance(r["purpose"], str):
            _bad(f"{what}: purpose must be a string")
        src = r["source"]
        if src is not None:
            if (not isinstance(src, dict) or set(src) != {"rel", "start", "end"}
                    or not isinstance(src["rel"], str) or _ABS_PATH_RE.search(src["rel"])
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
        m = f["max_ids"]
        if not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= MAX_FOLLOWUP_IDS:
            _bad(f"{what}: max_ids must be an integer 1..{MAX_FOLLOWUP_IDS}")
        if "headers" in f:
            _check_headers(f["headers"], what)

    local = []
    _walk_strings(plan["local_only"], local)
    _walk_strings(plan["excluded"], local)
    for s in local:
        if _ABS_PATH_RE.search(s):
            _bad("local_only/excluded entries must use paths relative to the scan root")
    # local-only data must not appear inside anything that is sent
    needles = [s for s in _values(plan["local_only"]) if len(s) >= _MIN_LEAK_LEN]
    if needles:
        sent_text = [r["url"] + "\n" + (r["body"] or "") for r in plan["requests"]]
        sent_text += [f["url_template"] for f in plan["followups"]]
        blob = "\n".join(sent_text)
        for n in needles:
            if n in blob:
                _bad("a local_only value appears in an outgoing request")


def _values(obj) -> list:
    out: list = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out += _values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out += _values(v)
    return out


# ---- build / strike ------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _root_name(root) -> str:
    text = str(root).replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1] if text else ""


def build_plan(backend: str, tier: str, root: str, requests: list, followups: list,
               disclosure: list, cost: dict, excluded: list, local_only: dict) -> dict:
    reqs = []
    for r in copy.deepcopy(list(requests)):
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
        "followups": copy.deepcopy(list(followups)),
        "disclosure": list(disclosure),
        "cost": copy.deepcopy(cost),
        "excluded": copy.deepcopy(list(excluded)),
        "local_only": copy.deepcopy(local_only),
    }
    validate_plan(plan)
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
    if isinstance(new["cost"].get("requests"), int):
        new["cost"]["requests"] = len(new["requests"])
    validate_plan(new)
    new["sha256"] = plan_hash(new)
    return new


# ---- preview ---------------------------------------------------------------------

def _hosts(plan: dict) -> list:
    hosts = set()
    for u in [r["url"] for r in plan["requests"]] + [f["url_template"] for f in plan["followups"]]:
        hosts.add(u.split("/", 3)[2])
    return sorted(hosts)


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
    for r in plan["requests"]:
        out.append(f"[{r['id']}] {r['method']} {r['url']}")
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
            line = f"  {f['method']} {f['url_template']}  (at most {f['max_ids']} ids per request)"
            out.append(line)
            if f.get("headers"):
                out.append("    headers: " + _fmt_headers(f["headers"]))
    out += ["", "Not sent: local file paths and names, file sizes, and any source text that is not "
                "shown in the requests above."]
    if plan["local_only"]:
        out.append(f"Kept on this machine only: {len(plan['local_only'])} local_only entries "
                   f"({', '.join(sorted(map(str, plan['local_only'])))}).")
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
    """Allows exactly the planned requests (and id-bearing follow-ups); logs what it allowed."""

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
            self._followups.append((f["method"], pre, suf, f["max_ids"], f.get("headers") or {}))
        self._allowed: set = set()
        self.sent: list = []

    def allow_ids(self, ids: Iterable[int]) -> None:
        batch = []
        for i in ids:
            if not isinstance(i, int) or isinstance(i, bool) or i < 0:
                raise ApprovalError("followup_not_allowed", "ids must be non-negative integers")
            batch.append(i)
        self._allowed.update(batch)

    def _match(self, method, url, body):
        """Return (header_spec, is_followup) or raise."""
        if (not isinstance(method, str) or not isinstance(url, str)
                or not (body is None or type(body) is bytes)):
            raise ApprovalError("request_not_in_plan", "malformed request")
        spec = self._index.get((method, url, body))
        if spec is not None:
            return spec, False
        error = None
        for fmethod, pre, suf, max_ids, fheaders in self._followups:
            if method != fmethod or body is not None:
                continue
            if len(url) < len(pre) + len(suf) or not url.startswith(pre) or not url.endswith(suf):
                continue
            mid = url[len(pre):len(url) - len(suf)]
            if not _IDS_RE.fullmatch(mid):
                error = error or ApprovalError("followup_not_allowed", "follow-up ids are malformed")
                continue
            tokens = mid.split(";")
            nums = [int(t) for t in tokens]
            if [str(n) for n in nums] != tokens or nums != sorted(set(nums)):
                error = error or ApprovalError("followup_not_allowed", "ids must be canonical, ascending, unique")
            elif len(nums) > max_ids:
                error = error or ApprovalError("followup_not_allowed", "too many ids in one follow-up")
            elif not set(nums) <= self._allowed:
                error = error or ApprovalError("followup_not_allowed", "id was not returned by the service")
            else:
                return fheaders, True
        if error:
            raise error
        raise ApprovalError("request_not_in_plan", f"{method} {_short(url)} is not in the approved plan")

    def planned_headers(self, method, url, body) -> dict:
        """Header spec ({name: literal | {"secret_from_env": VAR}}) of the matching plan entry."""
        return self._match(method, url, body)[0]

    def check(self, method: str, url: str, body, headers=None) -> None:
        spec, is_followup = self._match(method, url, body)
        if headers:
            if not isinstance(headers, dict):
                raise ApprovalError("request_not_in_plan", "headers must be a mapping")
            planned = {k.lower(): v for k, v in spec.items()}
            seen = set()
            for name, value in headers.items():
                if (not isinstance(name, str) or not isinstance(value, str)
                        or not _HEADER_NAME_RE.match(name) or _CONTROL_RE.search(value)):
                    raise ApprovalError("request_not_in_plan", "malformed header")
                low = name.lower()
                want = planned.get(low)
                if low in seen or want is None or isinstance(want, dict) or want != value:
                    raise ApprovalError("request_not_in_plan", f"header {_short(name, 40)!r} is not in the plan")
                seen.add(low)
        self.sent.append({
            "method": method, "url": url,
            "body_sha256": None if body is None else hashlib.sha256(body).hexdigest(),
            "body_bytes": 0 if body is None else len(body),
            "followup": is_followup,
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
            write_plan(new, Path(args.out))
            _emit(args.out, new)
    except ApprovalError as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
