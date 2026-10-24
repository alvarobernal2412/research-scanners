"""Open tier: Stack Exchange search for code taken from Stack Overflow answers.

PRIVACY. Only 1-3 rare identifiers (or hostnames / single words from string literals) per code block
leave the machine, as the `q=` parameter of `GET /search/excerpts`. Words that look like secrets, keys,
e-mail addresses, IP addresses, internal host names or paths are never used. Follow-up requests carry
only public post ids that the service returned for those searches (`/answers/{ids}`, then
`/posts/{ids}/revisions` for the answers that matched). Everything else happens locally: the answer
code is compared with your code by token comparison, and the licence is read per revision when the
service returns revision bodies.

`plan` writes the query file (every request verbatim, plus its sha256); `run` loads it with the approved
sha256 and sends only what it lists. The API key (`--use-key`) is never written to the plan: the plan
holds the marker `{secret:STACKEXCHANGE_KEY}` and the transport substitutes the environment value at
send time.

Quota: 300 requests/day anonymous, 10,000 with a key. The `backoff` field is honoured (bounded),
`quota_remaining: 0`, `error_id 502` and HTTP 429 stop the run and list everything not sent.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approval  # noqa: E402
import net  # noqa: E402
from approval import ApprovalError, Guard, build_plan, load_plan, render_preview, write_plan  # noqa: E402
from inventory import LANGUAGES, InventoryError, inventory, read_lines, resolve_includes  # noqa: E402
from model import Candidate, RawMatch, new_results, raw_match_to_dict, write_results  # noqa: E402
from normalize import Token, compare, token_line_span, tokenize  # noqa: E402
from replay import RecordingOpener  # noqa: E402
from scanoss_backend import _Reject, _bad_rel, _checked_bytes, _displayable, _prefixes, _under  # noqa: E402
from triage import local_match_drop_reason, split_blocks, trivial_reason  # noqa: E402

API = "https://api.stackexchange.com/2.3"
ANSWER_FILTER = "!aCuEwSk)swOnzkY756NSBElDuFY7vyPJwO83-S.Y8i"      # spike §2.1
KEY_TOKEN = "{secret:STACKEXCHANGE_KEY}"
SE_MAX_SEARCH_REQUESTS = 30
HARD_MAX_SEARCH_REQUESTS = 300
SE_MAX_ANSWERS_PER_BLOCK = 60         # two full result pages; spike §2.3: the right answer can rank 15-20 of 30, and ids are cheap (100 per request)
PAGE_SIZE = 30
FOLLOWUP_IDS = 100
MAX_BACKOFF = 120
PACE_SECONDS = 0.1                    # at most 10 requests/s, well below the documented 30/s ban line
MAX_EXCLUDED_LISTED = 1000
MAX_BLOCKS_PER_REQUEST = 50
MAX_CODE_BLOCKS_PER_ANSWER = 30
MAX_ANSWER_CODE_CHARS = 200_000
MAX_RESULTS_BYTES = 16 * 1024 * 1024
ANON_QUOTA = "300/day (anonymous)"
KEY_QUOTA = "10,000/day (with key)"
# creation date -> licence in force from that date (UTC); before the first date it is CC BY-SA 2.5
LICENSE_DATES = [("2011-04-08", "CC-BY-SA-3.0"), ("2018-05-02", "CC-BY-SA-4.0")]
_BEFORE_FIRST = "CC-BY-SA-2.5"

DISCLOSURE = [
    "Sent: for each code block you chose, 1 to 3 identifiers or words taken from your code (the rarest ones) "
    "as the q= parameter of a search request to api.stackexchange.com, a public service. Anyone who can see "
    "these requests can read those words: strike any request whose words look internal or private.",
    "Never used as query words: anything that looks like a secret, a key, an e-mail address, an IP address, an "
    "internal host name or a path. Not sent at all: file contents, file names or paths, comments, the project "
    "name.",
    "Follow-up requests carry only public post ids that the search returned (answers, then revisions of the "
    "answers that matched). They carry nothing from your code.",
    "Stack Exchange also sees your IP address, the time of each request and the client name. Without --use-key "
    "the requests are anonymous (no API key); the anonymous quota is 300 requests per day per IP.",
    "Matching happens on this machine: the returned answer code is compared with your code locally.",
]

_COMMON: frozenset | None = None
_SENSITIVE_WORDS = ("password", "passwd", "passphrase", "secret", "token", "apikey", "api_key", "api-key",
                    "credential", "privatekey", "private_key", "bearer", "authorization")
_PRIVATE_SUFFIXES = (".internal", ".local", ".corp", ".lan", ".intranet", ".private", ".home", ".localdomain",
                     ".localhost", ".intra", ".priv", ".test", ".invalid", ".example", ".cluster", ".svc")
_PRIVATE_LABELS = frozenset({"internal", "corp", "intranet", "intra", "localhost", "localdomain", "lan"})
_IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
_IPV6 = re.compile(r"(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")
_KEY_PREFIX = re.compile(r"(?:sk|pk|rk)_(?:live|test)_|gh[pousr]_|github_pat_|xox[abprs]-|AKIA[0-9A-Z]{8}|ASIA[0-9A-Z]{8}|"
                         r"AIza|eyJ[A-Za-z0-9_-]{5}|-----BEGIN|glpat-|npm_[A-Za-z0-9]{8}|sq0[a-z]{3}-")
_HEX_LONG = re.compile(r"[0-9a-fA-F]{32,}")
_HOST = re.compile(r"(?<![\w.-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,5}[A-Za-z]{2,24})(?![\w-])")
_FILE_EXTS = frozenset("""js jsx mjs ts tsx json py txt md html htm css scss csv tsv yml yaml xml jpg jpeg png gif svg
ico java c h cpp cc hpp cs go rs kt swift php scala dart sh bat ps1 log ini cfg conf toml lock pdf zip gz tar tgz exe
dll so class jar rb pl r sql map min env bak tmp""".split())
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{5,}")
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_CAMEL = re.compile(r"[a-z0-9][A-Z]|[A-Z]{2,}[a-z]")


class StackExchangeError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


# ---- identifiers --------------------------------------------------------------------------------

def load_common() -> frozenset:
    global _COMMON
    if _COMMON is None:
        data = json.loads((Path(__file__).resolve().parent.parent / "data" / "common_identifiers.json")
                          .read_text(encoding="utf-8"))
        words = set(data["common"])
        kw = json.loads((Path(__file__).resolve().parent.parent / "data" / "keywords.json")
                        .read_text(encoding="utf-8"))
        for family in kw.values():
            words.update(w.lower() for w in family)
        _COMMON = frozenset(words)
    return _COMMON


def _entropy(text: str) -> float:
    counts = Counter(text)
    n = len(text)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def looks_sensitive(token: str) -> bool:
    """True for anything that must never be a query word (fails closed)."""
    if not isinstance(token, str) or not token.strip():
        return True
    t = token.strip()
    low = t.lower()
    if any(w in low for w in _SENSITIVE_WORDS):
        return True
    if "@" in t or "/" in t or "\\" in t or "~" in t or " " in t or _EMAIL.search(t):
        return True
    if _IPV4.search(t) or (t.count(":") >= 2 and _IPV6.fullmatch(t)):
        return True
    if _KEY_PREFIX.match(t) or _HEX_LONG.search(t):
        return True
    if len(t) >= 20 and re.fullmatch(r"[A-Za-z0-9_+=-]+", t):
        digits = sum(c.isdigit() for c in t)
        if digits >= 3 or _entropy(t) > 4.0:
            return True
    if len(t) >= 12 and re.fullmatch(r"[A-Za-z0-9]+", t) and sum(c.isdigit() for c in t) >= 4 \
            and any(c.isalpha() for c in t) and _entropy(t) > 3.3:
        return True
    labels = low.split(".")
    if len(labels) > 1:
        if low.endswith(_PRIVATE_SUFFIXES) or any(lab in _PRIVATE_LABELS for lab in labels):
            return True
    if low in _PRIVATE_LABELS:
        return True
    return False


def _string_body(text: str) -> str:
    body = text
    m = re.match(r"[A-Za-z]{0,3}([\"'`])", body)
    if m:
        body = body[m.end():]
        if body.endswith(m.group(1)):
            body = body[:-1]
    return body


def _hosts_in(body: str) -> list:
    out = []
    for m in _HOST.finditer(body):
        host = m.group(1)
        tld = host.rsplit(".", 1)[-1].lower()
        if tld in _FILE_EXTS or looks_sensitive(host):
            continue
        out.append(host)
    return out


def rare_tokens(tokens: list, common: frozenset) -> list:
    """Query-word candidates, rarest first: hostnames from string literals, then camelCase / snake_case
    identifiers (length >= 6), then long lower-case identifiers; never common names or sensitive-looking words."""
    seen: dict = {}
    for order, t in enumerate(tokens):
        cands = []
        if t.kind == "string":
            body = _string_body(t.text)
            if looks_sensitive(body) and not _HOST.fullmatch(body):
                if any(w in body.lower() for w in _SENSITIVE_WORDS) or _KEY_PREFIX.match(body) \
                        or _EMAIL.search(body):
                    continue
            for host in _hosts_in(body):
                cands.append((0, host))
            if _WORD.fullmatch(body) and (_CAMEL.search(body) or "_" in body):
                cands.append((1, body))
        elif t.kind == "ident":
            text = t.text
            if len(text) >= 6 and _IDENT.fullmatch(text):
                if _CAMEL.search(text) or "_" in text.strip("_"):
                    cands.append((1, text))
                elif len(text) >= 8:
                    cands.append((2, text))
        for pri, text in cands:
            if text.lower() in common or looks_sensitive(text):
                continue
            key = (pri, -len(text), order)
            if text not in seen or key < seen[text]:
                seen[text] = key
    return [text for text, _ in sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))]


def build_queries(tokens: list) -> list:
    """Up to three queries per block, each of 1-3 rare words.

    q1 = the rarest single word; q2 = the two rarest together; q3 = the second rarest alone. q2 and q3 exist only
    when at least two rare words exist. q2 stops at two words (the search is AND, and a third word is often a name
    the user chose, which makes a renamed copy unfindable); q3 covers the case where the rarest word is not in the
    original (a rename) or is mangled by the search index (measured live 2026-10-24, C1p and C10)."""
    rare = rare_tokens(tokens, load_common())
    if not rare:
        return []
    queries = [rare[0]]
    if len(rare) >= 2:
        queries.append(" ".join(rare[:2]))
        queries.append(rare[1])
    return queries


def search_url(q: str, page: int = 1, key: str | None = None) -> str:
    if key is not None and key != KEY_TOKEN:
        raise ValueError("only the secret marker may be used as the key")
    url = f"{API}/search/excerpts?q={quote_plus(q, safe='')}&site=stackoverflow&pagesize={PAGE_SIZE}&page={page}"
    return url + (f"&key={key}" if key else "")


def _followup_templates(keyed: bool) -> list:
    suffix = f"&key={KEY_TOKEN}" if keyed else ""
    return [
        {"kind": "answers", "tpl": f"{API}/answers/{{ids}}?filter={ANSWER_FILTER}&site=stackoverflow&pagesize=100"
                                   + suffix},
        {"kind": "revisions", "tpl": f"{API}/posts/{{ids}}/revisions?filter=withbody&site=stackoverflow&pagesize=100"
                                     + suffix},
    ]


# ---- plan --------------------------------------------------------------------------------------

def _block_hash(lines: list) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _collides(url: str, local_only: dict) -> bool:
    mini = {"local_only": local_only, "requests": [{"url": url, "body": None}], "followups": []}
    try:
        approval._check_containment(mini)
    except ApprovalError:
        return True
    return bool(approval._has_absolute_path(unquote_all(url)))


def unquote_all(url: str) -> str:
    from urllib.parse import unquote_plus
    return unquote_plus(url)


def make_plan(root: Path, includes: list, use_key: bool = False, max_requests: int = SE_MAX_SEARCH_REQUESTS) -> dict:
    if not includes:
        raise StackExchangeError("include_required", "choose the files to search for with --include <path> [...]")
    if not isinstance(max_requests, int) or isinstance(max_requests, bool) or not 1 <= max_requests <= HARD_MAX_SEARCH_REQUESTS:
        raise StackExchangeError("max_requests_invalid", f"--max-requests must be 1..{HARD_MAX_SEARCH_REQUESTS}")
    if use_key and not os.environ.get("STACKEXCHANGE_KEY"):
        raise StackExchangeError("key_missing", "--use-key needs the STACKEXCHANGE_KEY environment variable")
    key = KEY_TOKEN if use_key else None
    inv = inventory(Path(root))
    chosen = resolve_includes(inv, list(includes))
    prefixes = _prefixes(inv, list(includes))
    excluded: list = [(s.rel, s.reason) for s in inv.skipped if _under(s.rel, prefixes)]

    candidates: list = []               # (rel, start, end, sha256, queries)
    for sf in chosen:
        if _bad_rel(sf.rel):
            excluded.append((sf.rel, "bad_name"))
            continue
        try:
            _checked_bytes(sf.path, inv.root, sf.rel)
        except _Reject as exc:
            excluded.append((sf.rel, exc.reason))
            continue
        lines = read_lines(sf.path)
        for block in split_blocks(sf, lines):
            label = f"{sf.rel}:{block.start}-{block.end}"
            reason = trivial_reason(block)
            if reason:
                excluded.append((label, reason))
                continue
            queries = build_queries(tokenize(block.lines, block.lang))
            if not queries:
                excluded.append((label, "no_rare_identifier"))
                continue
            candidates.append((sf.rel, block.start, block.end, _block_hash(block.lines), queries))

    rel_names = sorted({c[0] for c in candidates})
    probe_local = {"files": rel_names}
    requests: list = []
    by_url: dict = {}
    blocks_of: dict = {}
    full = False
    for rel, start, end, sha, queries in candidates:
        label = f"{rel}:{start}-{end}"
        if full:
            excluded.append((label, "cap"))
            continue
        urls = [(queries[0], 1), (queries[0], 2)] + [(q, 1) for q in queries[1:]]
        wanted = [(q, p, search_url(q, p, key)) for q, p in urls]
        bad = [w for w in wanted if _collides(w[2], probe_local)]
        if bad:
            excluded.append((label, "query_matches_local_path"))
            continue
        new = [w for w in wanted if w[2] not in by_url]
        if len(requests) + len(new) > max_requests:
            full = True
            excluded.append((label, "cap"))
            continue
        for q, p, url in wanted:
            if url not in by_url:
                rid = f"se-{len(requests) + 1}"
                words = q if len(q.split()) > 1 else q
                requests.append({"id": rid, "method": "GET", "url": url, "body": None,
                                 "purpose": f"search Stack Overflow answers for: {words} (page {p})",
                                 "source": {"rel": rel, "start": start, "end": end}})
                by_url[url] = rid
                blocks_of[rid] = []
            lst = blocks_of[by_url[url]]
            entry = {"rel": rel, "start": start, "end": end, "sha256": {"kind": "hash", "value": sha}}
            if len(lst) < MAX_BLOCKS_PER_REQUEST and not any(
                    (b["rel"], b["start"], b["end"]) == (rel, start, end) for b in lst):
                lst.append(entry)

    n = len(requests)
    nblocks = len({(b["rel"], b["start"], b["end"]) for bl in blocks_of.values() for b in bl})
    fu_requests = max(1, math.ceil(max(nblocks, 1) * SE_MAX_ANSWERS_PER_BLOCK / FOLLOWUP_IDS))
    followups = [{"method": "GET", "url_template": f["tpl"], "id_param": "ids", "max_ids": FOLLOWUP_IDS,
                  "max_requests": fu_requests} for f in _followup_templates(use_key)] if n else []
    disclosure = list(DISCLOSURE)
    quota_name = KEY_QUOTA if use_key else ANON_QUOTA
    if use_key:
        disclosure.append("These requests are keyed: your Stack Exchange API key (from STACKEXCHANGE_KEY) is added "
                          "to every request at send time. It is not stored in the plan or the results. The key "
                          "identifies your application to Stack Exchange.")
    else:
        disclosure.append("These requests carry no API key.")
    reasons = Counter(why for _rel, why in excluded)
    listed = [{"rel": rel if _displayable(rel.split(":")[0]) else "(bad name)", "reason": why}
              for rel, why in sorted(excluded)][:MAX_EXCLUDED_LISTED]
    if len(excluded) > MAX_EXCLUDED_LISTED:
        listed.append({"rel": f"({len(excluded) - MAX_EXCLUDED_LISTED} more)", "reason": "truncated_list"})
    quota = (f"{n} of {quota_name}; follow-up requests for the answers returned are extra "
             f"(at most {fu_requests * 2})")
    return build_plan(
        backend="stackexchange", tier="open", root=str(inv.root), requests=requests, followups=followups,
        disclosure=disclosure, cost={"requests": n, "quota": quota}, excluded=listed,
        local_only={"blocks": blocks_of})


# ---- answers, licences ------------------------------------------------------------------------------

_PRE = re.compile(r"<pre\b[^>]*>(.*?)</pre>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def answer_code_blocks(body_html: str) -> list:
    """Contents of `<pre>` blocks (tags stripped, entities unescaped)."""
    out = []
    for m in _PRE.finditer(body_html or ""):
        text = html.unescape(_TAG.sub("", m.group(1)))
        if text.strip():
            out.append(text.strip("\n"))
        if len(out) >= MAX_CODE_BLOCKS_PER_ANSWER:
            break
    return out


_FENCE = re.compile(r"^(```|~~~)")


def revision_code_blocks(body: str) -> list:
    """Code from a revision body: HTML `<pre>` blocks, or Markdown fenced / 4-space indented blocks."""
    if not isinstance(body, str):
        return []
    if re.search(r"<pre\b", body, re.I):
        return answer_code_blocks(body)
    out, cur, fenced = [], [], False
    for line in body.splitlines():
        if _FENCE.match(line.strip()):
            if fenced and cur:
                out.append("\n".join(cur))
                cur = []
            fenced = not fenced
            continue
        if fenced:
            cur.append(line)
        elif line.startswith(("    ", "\t")):
            cur.append(line[4:] if line.startswith("    ") else line[1:])
        else:
            if cur and not line.strip():
                cur.append("")
                continue
            if cur:
                out.append("\n".join(cur).strip("\n"))
                cur = []
    if cur:
        out.append("\n".join(cur).strip("\n"))
    return [b for b in out if b.strip()][:MAX_CODE_BLOCKS_PER_ANSWER]


def date_rule_license(creation_ts: int) -> str:
    when = datetime.fromtimestamp(int(creation_ts), tz=timezone.utc).strftime("%Y-%m-%d")
    spdx = _BEFORE_FIRST
    for start, lic in LICENSE_DATES:
        if when >= start:
            spdx = lic
    return spdx


_CC = re.compile(r"CC[ -]BY-SA[ -](\d\.\d)", re.I)


def spdx_from_content_license(text) -> str | None:
    m = _CC.search(text) if isinstance(text, str) else None
    return f"CC-BY-SA-{m.group(1)}" if m else None


def _code_tokens(body_fn, text, lang):
    return [tokenize(code.splitlines(), lang) for code in body_fn(text)[:MAX_CODE_BLOCKS_PER_ANSWER]]


def _best_local(user_tokens: list, code_blocks: list, lang: str):
    """Best LocalMatch of the user's tokens against any code block of a post (or all blocks joined)."""
    sets = [tokenize(c[:MAX_ANSWER_CODE_CHARS].splitlines(), lang) for c in code_blocks]
    if len(code_blocks) > 1:
        sets.append(tokenize("\n".join(code_blocks)[:MAX_ANSWER_CODE_CHARS].splitlines(), lang))
    best = None
    for toks in sets:
        m = compare(user_tokens, toks)
        if m is not None and (best is None or (m.coverage, m.tokens) > (best.coverage, best.tokens)):
            best = m
    return best


def _lic(spdx, evidence, confidence, status="declared"):
    return {"spdx": spdx, "status": status, "evidence": evidence, "confidence": confidence}


def resolve_license(answer: dict, revisions: list | None, user_tokens: list, lang: str = "c_like") -> list:
    """Licence of the answer text that holds the user's code. The earliest revision whose code matches wins
    (`so_revision`, high); otherwise the answer's current `content_license` (`so_answer_current`, medium) and,
    when it differs, the date rule (`so_date_rule`, low)."""
    with_body = [r for r in (revisions or []) if isinstance(r, dict) and isinstance(r.get("body"), str)]
    for rev in sorted(with_body, key=lambda r: (r.get("creation_date") or 0, r.get("revision_number") or 0)):
        blocks = revision_code_blocks(rev["body"])
        if blocks and _best_local(user_tokens, blocks, lang) is not None:
            spdx = spdx_from_content_license(rev.get("content_license"))
            out = _lic(spdx, "so_revision", "high", "declared" if spdx else "unknown")
            out["revision"] = str(rev.get("revision_number")) if rev.get("revision_number") is not None else None
            return [out]
    out = []
    cur = spdx_from_content_license(answer.get("content_license"))
    out.append(_lic(cur, "so_answer_current", "medium", "declared" if cur else "unknown"))
    created = answer.get("creation_date")
    if isinstance(created, int) and not isinstance(created, bool):
        rule = date_rule_license(created)
        if rule != cur:
            out.append(_lic(rule, "so_date_rule", "low"))
    return out


# ---- run -----------------------------------------------------------------------------------------

def _error(code, detail="", request_id=None):
    err = {"code": code, "detail": detail}
    if request_id:
        err["request_id"] = request_id
    return err


def _short(exc) -> str:
    return type(exc).__name__ if not getattr(exc, "detail", "") else str(exc.detail)[:200]


class _Session:
    def __init__(self, guard, res, opener, sleep):
        self.guard, self.res, self.opener, self.sleep = guard, res, opener, sleep
        self.stopped = False
        self.stop_reported = False
        self.backoff = 0
        self.sent_any = False

    def not_sent(self, rid, url, reason):
        self.res["not_sent"].append({"id": rid, "method": "GET", "url": url, "reason": reason})

    def get(self, rid: str, url: str):
        """Send one planned request. Returns the decoded JSON wrapper, or None (already recorded)."""
        if self.stopped:
            self.not_sent(rid, url, "quota_exhausted")
            if not self.stop_reported:
                self.stop_reported = True
                self.res["errors"].append(_error("quota_exhausted", "the Stack Exchange quota or rate limit was "
                                                                    "reached; later requests were not sent", rid))
            return None
        if self.backoff:
            self.sleep(min(self.backoff, MAX_BACKOFF))
            self.backoff = 0
        elif self.sent_any:
            self.sleep(PACE_SECONDS)
        self.sent_any = True
        try:
            resp = net.send("GET", url, self.guard, opener=self.opener, sleep=self.sleep)
        except Exception as exc:                      # NetError, ApprovalError, blocked sockets, ...
            code = exc.code if isinstance(getattr(exc, "code", None), str) else "network_error"
            self.res["errors"].append(_error(code, _short(exc), rid))
            self.not_sent(rid, url, code)
            return None
        try:
            data = json.loads(resp.body.decode("utf-8-sig")) if resp.body else None
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("backoff"), int) and not isinstance(data["backoff"], bool) \
                and data["backoff"] > 0:
            self.backoff = data["backoff"]
        throttled = isinstance(data, dict) and data.get("error_id") == 502
        if resp.status == 429 or throttled:
            self.stopped = self.stop_reported = True
            self.res["errors"].append(_error("quota_exhausted", "Stack Exchange answered "
                                             + ("HTTP 429" if resp.status == 429 else "error 502 (throttle_violation)"),
                                             rid))
            self.not_sent(rid, url, "quota_exhausted")
            return None
        if resp.status != 200:
            code = "service_unavailable" if resp.status >= 500 else f"http_{resp.status}"
            name = data.get("error_name") if isinstance(data, dict) else None
            self.res["errors"].append(_error(code, f"HTTP {resp.status}" + (f" {str(name)[:60]}" if name else ""), rid))
            self.not_sent(rid, url, code)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            self.res["errors"].append(_error("bad_response", "the response is not the expected JSON", rid))
            self.not_sent(rid, url, "bad_response")
            return None
        if data.get("quota_remaining") == 0:
            self.stopped = True
        return data


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _load_block(root: Path, b: dict):
    """(user tokens, lang, lines) for a planned block, or None when the file changed since the plan."""
    rel = b["rel"]
    path = root / rel
    try:
        _checked_bytes(path, root, rel)
        lines = read_lines(path)
    except (_Reject, OSError):
        return None
    chunk = lines[b["start"] - 1:b["end"]]
    expected = b["sha256"]["value"] if isinstance(b.get("sha256"), dict) else b.get("sha256")
    if len(chunk) != b["end"] - b["start"] + 1 or _block_hash(chunk) != expected:
        return None
    lang = LANGUAGES.get(Path(rel).suffix.lower().lstrip("."))
    if lang is None:
        return None
    return tokenize(chunk, lang), lang, chunk


def _iso_date(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _s(value, limit=200):
    return value[:limit] if isinstance(value, str) else None


def _owner(answer: dict):
    owner = answer.get("owner") if isinstance(answer.get("owner"), dict) else {}
    link = _s(owner.get("link"), 300)
    return _s(owner.get("display_name"), 120), link if link and link.startswith("https://") else None


def run(plan_path: Path, approved_sha256: str, root: Path, opener=None, sleep=time.sleep) -> dict:
    plan = load_plan(plan_path, approved_sha256)          # raises ApprovalError before anything is sent
    root = Path(root)
    if not root.is_dir() or root.resolve().name != plan["root"]:
        raise StackExchangeError("root_mismatch", "--root must be the directory the plan was made for")
    root = root.resolve()
    guard = Guard(plan)
    res = new_results("stackexchange", "open", plan["sha256"], plan["root"])
    sess = _Session(guard, res, opener, sleep)
    local = plan["local_only"].get("blocks") if isinstance(plan["local_only"].get("blocks"), dict) else {}

    loaded: dict = {}                    # (rel, start, end) -> (tokens, lang, lines) or None
    changed: set = set()

    def block_state(b):
        key = (b["rel"], b["start"], b["end"])
        if key not in loaded:
            loaded[key] = _load_block(root, b)
            if loaded[key] is None:
                changed.add(key)
        return loaded[key]

    block_cands: dict = OrderedDict()    # key -> [(result count, rank, answer id)]
    complete: set = set()                # queries whose page 1 was complete
    page2_skipped = 0
    searched = 0
    for req in plan["requests"]:
        blocks = [b for b in local.get(req["id"], []) if isinstance(b, dict)]
        live = [b for b in blocks if block_state(b) is not None]
        if blocks and not live:
            sess.not_sent(req["id"], req["url"], "file_changed_since_plan")
            continue
        qs = parse_qs(urlsplit(req["url"]).query)
        q = (qs.get("q") or [""])[0]
        page = (qs.get("page") or ["1"])[0]
        if page != "1" and q in complete:
            page2_skipped += 1
            continue
        data = sess.get(req["id"], req["url"])
        if data is None:
            continue
        searched += 1
        ids = [it["answer_id"] for it in data["items"]
               if isinstance(it, dict) and it.get("item_type", "answer") == "answer"
               and type(it.get("answer_id")) is int and it["answer_id"] >= 1]
        if page == "1" and not data.get("has_more"):
            complete.add(q)
        try:
            guard.allow_ids(ids)
        except ApprovalError:
            ids = []
        for b in live:
            lst = block_cands.setdefault((b["rel"], b["start"], b["end"]), [])
            lst.extend((len(ids), rank, i) for rank, i in enumerate(ids))
    # a narrow query's results come first, so a broad query cannot fill the per-block cap with noise
    block_ids: dict = OrderedDict()
    for key, cands in block_cands.items():
        picked: list = []
        for _n, _rank, i in sorted(cands):
            if i not in picked and len(picked) < SE_MAX_ANSWERS_PER_BLOCK:
                picked.append(i)
        block_ids[key] = picked

    # ---- fetch the candidate answers (ids returned by the searches only)
    templates = {("answers" if "/answers/" in f["url_template"] else "revisions"): f["url_template"]
                 for f in plan["followups"]}
    wanted = sorted({i for ids in block_ids.values() for i in ids})
    answers: dict = {}
    if wanted and "answers" in templates:
        for n, chunk in enumerate(_chunks(wanted, FOLLOWUP_IDS), 1):
            url = templates["answers"].replace("{ids}", ";".join(map(str, chunk)))
            data = sess.get(f"answers-{n}", url)
            if data is None:
                continue
            for it in data["items"]:
                if isinstance(it, dict) and type(it.get("answer_id")) is int:
                    answers[it["answer_id"]] = it

    # ---- compare locally
    hits: list = []                       # (key, answer, local match, tokens, lang)
    for key, ids in block_ids.items():
        state = loaded.get(key)
        if state is None:
            continue
        user_tokens, lang, _chunk = state
        for i in ids:
            ans = answers.get(i)
            if not ans:
                continue
            codes = answer_code_blocks(ans.get("body", ""))
            if not codes:
                continue
            m = _best_local(user_tokens, codes, lang)
            if m is None:
                continue
            reason = local_match_drop_reason(m, len(user_tokens))
            if reason:
                res["dropped"].append({"rel": key[0], "start": key[1], "end": key[2], "reason": reason})
                continue
            hits.append((key, ans, m, user_tokens, lang))

    # ---- revisions of the answers that matched (licence per revision)
    revisions: dict = {}
    matched_ids = sorted({h[1]["answer_id"] for h in hits})
    if matched_ids and "revisions" in templates:
        for n, chunk in enumerate(_chunks(matched_ids, FOLLOWUP_IDS), 1):
            url = templates["revisions"].replace("{ids}", ";".join(map(str, chunk)))
            data = sess.get(f"revisions-{n}", url)
            if data is None:
                continue
            for it in data["items"]:
                if isinstance(it, dict) and type(it.get("post_id")) is int:
                    revisions.setdefault(it["post_id"], []).append(it)

    for key, ans, m, user_tokens, lang in sorted(hits, key=lambda h: (h[0], h[1]["answer_id"])):
        pid = ans["answer_id"]
        rel, start, _end = key
        revs = revisions.get(pid)
        licenses = resolve_license(ans, revs, user_tokens, lang)
        revision = licenses[0].get("revision") if licenses and licenses[0]["evidence"] == "so_revision" else None
        lic_out = [{k: v for k, v in lic.items() if k != "revision"} for lic in licenses]
        l0, l1 = token_line_span(user_tokens, m.a_start, m.a_end)
        author, author_url = _owner(ans)
        if revision:
            note = f"Matched revision {revision} of the answer; the licence is that revision's."
        else:
            note = ("Matched the current text of the answer; Stack Overflow licences are per revision, so the "
                    "licence shown is the answer's current one" +
                    (" and the date rule is listed as a fallback." if len(lic_out) > 1 else "."))
        cand = Candidate(origin_url=f"https://stackoverflow.com/a/{pid}", kind="stackoverflow", backend="stackexchange",
                         author=author, author_url=author_url, date=_iso_date(ans.get("creation_date")),
                         revision=revision, licenses=lic_out, matched_pct=round(m.coverage * 100, 1),
                         purl=[], post_id=pid, note=note)
        res["matches"].append(raw_match_to_dict(RawMatch(rel=rel, start=start + l0 - 1, end=start + l1 - 1,
                                                         match_type=m.match_type, candidate=cand)))

    res["requests_sent"] = list(guard.sent)
    for key in sorted(changed):
        res["notes"].append(f"file_changed_since_plan: {key[0]} lines {key[1]}-{key[2]} changed after the plan was "
                            "made; nothing was sent for it and it was not checked.")
    if page2_skipped:
        res["notes"].append(f"{page2_skipped} planned page 2 request(s) were not needed: page 1 held all results "
                            "(not an error).")
    res["notes"] += _notes(plan, res, searched, len(block_ids), len(hits))
    return res


def _notes(plan: dict, res: dict, searched: int, nblocks: int, nhits: int) -> list:
    notes = []
    if not plan["requests"]:
        notes.append("nothing_to_look_up: the plan has no requests, so no Stack Overflow search was made.")
    notes.append("Open tier: Stack Overflow answers found by keyword search on 1-3 rare identifiers per code block, "
                 "then compared locally. Code without a rare identifier, trivial blocks, and blocks outside your "
                 "--include paths were not searched.")
    notes.append("Results are candidates, never a single established origin; similar code can exist in several "
                 "places and similarity cannot show who copied from whom.")
    if plan["requests"] and not res["errors"] and not nhits:
        notes.append("no_match: no Stack Overflow answer with matching code was found for the searched blocks; this "
                     "does not show that the code is original (search is keyword-based and can miss).")
    if res["errors"]:
        notes.append("The search was incomplete: the blocks whose requests were not sent were NOT checked. See "
                     "coverage errors and not_sent.")
    by_reason = Counter(e.get("reason") for e in plan["excluded"]
                        if isinstance(e.get("reason"), str))
    if by_reason:
        res["excluded_by_reason"] = dict(sorted(by_reason.items()))
        notes.append("not searched, by reason: " + ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
    return notes


# ---- CLI ---------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": "usage", "detail": message}))
        raise SystemExit(2)


def main(argv: list) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = _Parser(prog="stackexchange.py")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    p = sub.add_parser("plan")
    p.add_argument("root")
    p.add_argument("--include", nargs="+", action="extend", default=[])
    p.add_argument("--out", required=True)
    p.add_argument("--use-key", action="store_true")
    p.add_argument("--max-requests", type=int, default=SE_MAX_SEARCH_REQUESTS)
    r = sub.add_parser("run")
    r.add_argument("plan")
    r.add_argument("--sha256", required=True)
    r.add_argument("--root", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--record")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            plan = make_plan(Path(args.root), args.include, use_key=args.use_key, max_requests=args.max_requests)
            write_plan(plan, Path(args.out), exclusive=True)
            print(json.dumps({"plan_path": str(Path(args.out).resolve()), "sha256": plan["sha256"],
                              "preview": render_preview(plan), "excluded": plan["excluded"]}, ensure_ascii=False))
            return 0
        opener = RecordingOpener(net.build_default_opener(), Path(args.record)) if args.record else None
        try:
            res = run(Path(args.plan), args.sha256, Path(args.root), opener=opener)
        except (ApprovalError, StackExchangeError) as exc:
            res = new_results("stackexchange", "open", args.sha256, "")
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
                          "requests_sent": len(res["requests_sent"]), "notes": res["notes"]}))
        return 0
    except StackExchangeError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail or str(exc)}))
    except (ApprovalError, InventoryError) as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}))
    except (OSError, ValueError):
        print(json.dumps({"error": "output_error", "detail": "file system or encoding error"}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
