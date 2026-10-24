"""Open tier: send approved exact phrases from chosen sections to open search backends (opt-in per run).

    python -I open_tier.py plan --workdir W --sections s2,s3 [--backends europepmc,openalex] [--cap N]
                                [--include c4,c9] [--use-keys]
    python -I open_tier.py plan --workdir W            next round: second OpenAlex window, then verification
    python -I open_tier.py run  --workdir W --plan P --sha256 H

`plan` prints the exact phrases grouped by service, the sentences it excluded and why, and the sha256 of
the plan; `run` sends only what that plan lists. Sentences with digits, equations or novel terms are
excluded unless re-added by id; at most OPEN_QUERY_CAP sentences and OPEN_BACKEND_CAP requests per backend
are sent per run, prioritised by distinctiveness round-robin across sections; the rest is counted, not hidden.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import be_europepmc  # noqa: E402
from approval import ApprovalError, Gate, build_plan, load_approved_plan, preview, write_plan  # noqa: E402
from attribute import quote_mask  # noqa: E402
from common import configure_stdout, emit, fail  # noqa: E402
from net import Cache, RequestLog, cache_root, send  # noqa: E402
from prepare import load_doc  # noqa: E402
from sections import EXCLUDED_KINDS, default_open_sections, excluded_lines  # noqa: E402
from textnorm import STOPWORDS, split_sentences, tokenize  # noqa: E402

OPEN_QUERY_CAP = 10
OPEN_BACKEND_CAP = 10
OPENALEX_MIN_REMAINING = 10
SENTENCE_MIN_WORDS = 12
SENTENCE_MAX_WORDS = 40
QUOTED_FRACTION = 0.8

BACKENDS: dict[str, object] = {"europepmc": be_europepmc}
# CORE answered with 500/429 and no verified body-text hit in the spike: opt-in only, as are Open Library and Brave
DEFAULT_BACKENDS = ("openalex", "europepmc")
for _name, _mod in (("openalex", "be_openalex"), ("core", "be_core"), ("openlibrary", "be_books_web"),
                    ("brave", "be_books_web")):
    try:
        _m = __import__(_mod)
        BACKENDS[_name] = getattr(_m, _name) if _mod == "be_books_web" else _m
    except ImportError:
        pass

REASON_KEEP = ("numbers", "equation", "novel_term", "quoted")


@dataclass
class Candidate:
    id: str
    section_id: str
    line_start: int
    line_end: int
    text: str
    score: float
    excluded: str | None


_COMMON: set[str] | None = None


def common_words() -> set[str]:
    global _COMMON
    if _COMMON is None:
        p = Path(__file__).resolve().parent.parent / "data" / "common_words.txt"
        try:
            _COMMON = {w.strip().lower() for w in p.read_text(encoding="utf-8").splitlines() if w.strip()}
        except OSError:
            _COMMON = set()
    return _COMMON


def distinctiveness(keys: list[str]) -> float:
    content = [k for k in keys if k not in STOPWORDS]
    if not content:
        return 0.0
    common = common_words()
    return sum(1 for k in content if len(k) >= 7 and k.isalpha() and k not in common) / len(content)


_CITATION = re.compile(r"\[[\d,;\s–\-]+\]|\((?:[^()]*?\b(?:1[5-9]|20)\d\d[a-z]?)\)")
_STRIP_CHARS = re.compile(r"[\"“”„«»\[\]\(\)\{\}:;]")
_EQUATION = re.compile(r"[=<>±×∑∫\\$^_]|[Ͱ-Ͽ]")
_ACRONYM_DEF = re.compile(r"\b[A-Z][A-Za-z\-]+(?:\s+[A-Za-z\-]+){0,6}\s*\(([A-Z][A-Za-z0-9]{1,9})\)")
_INTRODUCED = re.compile(r"\b(?:we\s+call|we\s+term|we\s+name|referred\s+to\s+as|dubbed)\s+(?:it\s+|this\s+|them\s+)?"
                         r"[\"“‘']?([A-Za-z][\w\-]*(?:\s+[A-Za-z][\w\-]*){0,2})", re.I)


def phrase_for_query(text: str, backend: str = "europepmc") -> str:
    s = _CITATION.sub(" ", text)
    s = _STRIP_CHARS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if backend == "openalex":
        try:
            import be_openalex

            return be_openalex.window_phrase(s)
        except ImportError:
            pass
    return s


def _novel_terms(full_text: str) -> tuple[set[str], list[str]]:
    acronyms = set(_ACRONYM_DEF.findall(full_text))
    introduced = []
    for m in _INTRODUCED.finditer(full_text):
        words = m.group(1).split()
        for n in (3, 2, 1):                # the introduced term may be 1-3 words; keep the shortest unambiguous
            introduced.append(" ".join(words[:n]).lower())
            break
    return acronyms, [t for t in introduced if len(t) >= 4]


def all_candidates(workdir: Path) -> tuple[list[Candidate], dict]:
    """Every body sentence of the document with its exclusion reason (None = eligible)."""
    doc, sections, _ = load_doc(workdir)
    tokens = tokenize(doc.lines)
    excl_lines = excluded_lines(doc, sections)
    sentences = split_sentences(doc.lines, tokens)
    mask = quote_mask(doc.lines, tokens)
    full = " ".join(l.text for l in doc.lines if l.no not in excl_lines)
    acronyms, introduced = _novel_terms(full)
    heading_lines = {l.no for l in doc.lines if l.heading_level is not None}
    by_section = {}
    for s in sections:
        for l in doc.lines:
            if s.start_line <= l.no <= s.end_line:
                by_section[l.no] = s
    out: list[Candidate] = []
    n = 0
    for sent in sentences:
        if sent.line_start in heading_lines and sent.line_end in heading_lines:
            continue
        sec = by_section.get(sent.line_start)
        if sec is None or sec.kind in EXCLUDED_KINDS or sent.line_start in excl_lines:
            n += 1          # keep ids stable, never a candidate
            continue
        n += 1
        toks = tokens[sent.start_tok:sent.end_tok]
        keys = [t.key for t in toks]
        text = sent.text
        clean = _CITATION.sub(" ", text)
        words = len(keys)
        reason = None
        if words < SENTENCE_MIN_WORDS:
            reason = "too_short"
        elif words > SENTENCE_MAX_WORDS:
            reason = "too_long"
        elif sum(1 for m in mask[sent.start_tok:sent.end_tok] if m) / words >= QUOTED_FRACTION:
            reason = "quoted"
        elif any(c.isdigit() for c in clean):
            reason = "numbers"
        elif _EQUATION.search(clean):
            reason = "equation"
        else:
            raw_words = re.findall(r"[A-Za-z][\w\-]*", clean)
            lowered = clean.lower()
            if (any(re.fullmatch(r"[a-z]+[A-Z][A-Za-z]*|[A-Z]{2,}[a-z]*|[A-Z][a-z]+[A-Z][A-Za-z]*", w) for w in raw_words)
                    or any(a in raw_words for a in acronyms) or any(t in lowered for t in introduced)):
                reason = "novel_term"
        out.append(Candidate(f"c{n}", sec.id, sent.line_start, sent.line_end, text, distinctiveness(keys), reason))
    return out, {"sections": sections}


def select_sentences(workdir: Path, section_ids: list[str], include: set[str] | None = None,
                     cap: int = OPEN_QUERY_CAP) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    include = include or set()
    cands, _ = all_candidates(workdir)
    chosen = [c for c in cands if c.section_id in set(section_ids)]
    eligible, excluded = [], []
    for c in chosen:
        if c.excluded is None or (c.id in include and c.excluded in REASON_KEEP):
            eligible.append(c)
        else:
            excluded.append(c)
    readded = [c for c in eligible if c.id in include]
    rest = [c for c in eligible if c.id not in include]
    per_section: dict[str, list[Candidate]] = {}
    for c in sorted(rest, key=lambda c: (-c.score, c.line_start)):
        per_section.setdefault(c.section_id, []).append(c)
    selected = list(readded)[:cap]
    order = [s for s in section_ids if s in per_section]
    over: list[Candidate] = []
    queues = {s: list(per_section[s]) for s in order}
    while queues and len(selected) < cap:
        for s in list(queues):
            if len(selected) >= cap:
                break
            q = queues[s]
            selected.append(q.pop(0))
            if not q:
                del queues[s]
    for q in queues.values():
        over.extend(q)
    over.extend(readded[cap:])
    return selected, excluded, over


# ----------------------------------------------------------------- state

def _state_path(workdir: Path) -> Path:
    return Path(workdir) / "open_state.json"


def load_open_state(workdir: Path) -> dict | None:
    p = _state_path(workdir)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _save(workdir: Path, state: dict) -> None:
    _state_path(workdir).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _next_plan_path(workdir: Path, round_name: str) -> Path:
    d = Path(workdir) / "plans"
    n = len(list(d.glob("open-*.json"))) + 1 if d.is_dir() else 1
    return d / f"open-{n:02d}-{round_name}.json"


def _emit_plan(workdir: Path, plan: dict, round_name: str, extra: dict) -> int:
    path = _next_plan_path(workdir, round_name)
    sha = write_plan(plan, path)
    emit({"round": round_name, "plan": str(path), "sha256": sha, "preview": preview(plan), **extra})
    return 0


# ----------------------------------------------------------------- plan

def _build_search_requests(selected: list[Candidate], backends: list[str], use_keys: bool, windows: dict | None = None):
    reqs = []
    skipped: Counter = Counter()
    counts: Counter = Counter()
    for c in selected:
        for name in backends:
            be = BACKENDS[name]
            phrase = phrase_for_query(c.text, name)
            if windows and name in windows.get(c.id, {}):
                phrase = windows[c.id][name]
            vs = getattr(be, "variants", lambda p: [p])(phrase)
            if counts[name] + len(vs) > OPEN_BACKEND_CAP:
                skipped[name] += 1
                continue
            for v in vs:
                r = be.build_search(v, "", use_keys)
                r.subject = f"cand:{c.id}"
                reqs.append(r)
                counts[name] += 1
    return reqs, skipped


def cmd_plan(workdir: Path, sections: list[str] | None, backends: list[str] | None, cap: int,
             include: set[str], use_keys: bool) -> int:
    if not (workdir / "doc.json").is_file():
        return fail("workdir_missing", str(workdir))
    doc, secs, _ = load_doc(workdir)
    state = load_open_state(workdir)
    if sections is None and state and state.get("results"):
        return _plan_followup(workdir, doc, state)
    if sections is None:
        sections = default_open_sections(secs)
    known = {s.id for s in secs}
    bad = [s for s in sections if s not in known]
    if bad:
        return fail("unknown_section", ",".join(bad))
    names = list(backends) if backends else [b for b in DEFAULT_BACKENDS if b in BACKENDS]
    for n in names:
        if n not in BACKENDS:
            return fail("unknown_backend", n)
        be = BACKENDS[n]
        if getattr(be, "KEYED_ONLY", False) and not os.environ.get(getattr(be, "KEY_ENV", "") or ""):
            return fail("missing_key", getattr(be, "KEY_ENV", n))
    selected, excluded, over = select_sentences(workdir, sections, include, cap)
    if not selected or not names:
        return fail("nothing_to_plan", "no eligible sentences in the chosen sections")
    reqs, skipped = _build_search_requests(selected, names, use_keys)
    plan = build_plan("open", "search", doc.sha256, reqs)
    over_by_section: Counter = Counter(c.section_id for c in over)
    _save(workdir, {"version": 1, "use_keys": use_keys,
                    "selection": {"sections": sections, "backends": names, "cap": cap,
                                  "candidates": [asdict(c) for c in selected],
                                  "excluded": [asdict(c) for c in excluded],
                                  "over_cap": dict(over_by_section),
                                  "backend_over_cap": dict(skipped)},
                    "queries": {}, "results": {}, "rounds": []})
    return _emit_plan(workdir, plan, "search", {
        "selected": [{"id": c.id, "section": c.section_id, "text": c.text} for c in selected],
        "excluded": [{"id": c.id, "reason": c.excluded, "text": c.text} for c in excluded
                     if c.excluded in REASON_KEEP],
        "excluded_by_length": sum(1 for c in excluded if c.excluded in ("too_short", "too_long")),
        "over_cap": dict(over_by_section), "backend_over_cap": dict(skipped)})


def _plan_followup(workdir: Path, doc, state: dict) -> int:
    done_rounds = {r["round"] for r in state.get("rounds", [])}
    cands = {c["id"]: c for c in state["selection"]["candidates"]}
    # second OpenAlex window for candidates whose first window found nothing
    if "search2" not in done_rounds and "openalex" in BACKENDS and "openalex" in state["selection"]["backends"]:
        import be_openalex

        windows = {}
        for rid, q in state["queries"].items():
            res = state["results"].get(rid, {})
            if q["backend"] == "openalex" and res.get("hit_count") == 0 and not res.get("error"):
                second = be_openalex.second_window(phrase_for_query(cands[q["candidate"]]["text"], "plain"))
                if second:
                    windows.setdefault(q["candidate"], {})["openalex"] = second
        if windows:
            selected = [Candidate(**cands[cid]) for cid in windows]
            reqs, _ = _build_search_requests(selected, ["openalex"], state.get("use_keys", False), windows)
            if reqs:
                return _emit_plan(workdir, build_plan("open", "search2", doc.sha256, reqs), "search2",
                                  {"selected": [{"id": c.id, "text": c.text} for c in selected]})
    if "verify" not in done_rounds:
        import verify

        reqs = verify.plan_verification(state)
        if reqs:
            return _emit_plan(workdir, build_plan("open", "verify", doc.sha256, reqs), "verify", {})
    emit({"done": True})
    return 0


# ----------------------------------------------------------------- run

def cmd_run(workdir: Path, plan_path: Path, sha256: str, opener=None) -> int:
    if not (workdir / "doc.json").is_file():
        return fail("workdir_missing", str(workdir))
    doc, _, _ = load_doc(workdir)
    try:
        plan = load_approved_plan(plan_path, sha256, doc.sha256)
    except ApprovalError as exc:
        return fail(exc.code, exc.detail or None)
    if plan["tier"] != "open":
        return fail("plan_invalid", "not an open-tier plan")
    state = load_open_state(workdir)
    if state is None:
        return fail("plan_missing", "no open-tier selection recorded; run plan first")
    gate = Gate(plan)
    sleep = time.sleep if opener is None else (lambda s: None)
    cache = Cache(cache_root())
    log = RequestLog(workdir / "requests.jsonl")
    round_name = plan["round"]
    counts: Counter = Counter()
    errors: Counter = Counter()
    exhausted: set[str] = set()
    for req in gate.requests():
        service = req.service
        if round_name in ("search", "search2"):
            be = BACKENDS.get(service)
            if service in exhausted:
                _record(state, req, service, None, [], "budget_exhausted")
                errors["budget_exhausted"] += 1
                continue
        resp = send(req, gate, tier="open", round_name=round_name, opener=opener, cache=cache, log=log, sleep=sleep)
        counts["cached" if resp.from_cache else "sent"] += 1
        if resp.error:
            errors[resp.error] += 1
        elif resp.status == 200:
            counts["ok"] += 1
        if round_name in ("search", "search2"):
            be = BACKENDS[service]
            if resp.error or resp.status != 200:
                _record(state, req, service, None, [], resp.error or f"http_{resp.status}")
            else:
                try:
                    qid = req.subject.split(":", 1)[1]
                    count, hits = be.parse_search(resp.body, qid)
                    _record(state, req, service, count, hits, None)
                except (ValueError, KeyError, UnicodeDecodeError):
                    _record(state, req, service, None, [], "parse_error")
                    errors["parse_error"] += 1
            budget = getattr(be, "budget_remaining", None)
            if budget is not None:
                left = budget(resp.headers)
                if left is not None and left < OPENALEX_MIN_REMAINING:
                    exhausted.add(service)
        else:
            import verify

            verify.apply_fetch(state, req, resp, workdir)
    state.setdefault("rounds", []).append({"round": round_name, "plan": plan_path.name, "sha256": sha256})
    _save(workdir, state)
    emit({"round": round_name, "sent": counts["sent"], "ok": counts["ok"], "cached": counts["cached"],
          "errors": dict(errors)})
    return 0


def _record(state: dict, req, service: str, count, hits, error) -> None:
    key = f"{len(state.get('rounds', []))}:{req.id}"      # unique across rounds
    state["queries"][key] = {"candidate": req.subject.split(":", 1)[1], "backend": service, "text": req.text_sent,
                             "req_id": req.id}
    state["results"][key] = {"hit_count": count, "hits": [asdict(h) for h in hits], "error": error}


def main(argv: list[str] | None = None, opener=None) -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(prog="open_tier.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--workdir", required=True)
    p.add_argument("--sections")
    p.add_argument("--backends")
    p.add_argument("--cap", type=int, default=OPEN_QUERY_CAP)
    p.add_argument("--include", default="")
    p.add_argument("--use-keys", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--workdir", required=True)
    r.add_argument("--plan", required=True)
    r.add_argument("--sha256", required=True)
    args = ap.parse_args(argv)
    workdir = Path(args.workdir)
    if not workdir.is_dir():
        return fail("workdir_missing", str(workdir))
    if args.cmd == "plan":
        return cmd_plan(workdir,
                        [s.strip() for s in args.sections.split(",") if s.strip()] if args.sections else None,
                        [b.strip() for b in args.backends.split(",") if b.strip()] if args.backends else None,
                        min(args.cap, OPEN_QUERY_CAP),
                        {i.strip() for i in args.include.split(",") if i.strip()}, args.use_keys)
    return cmd_run(workdir, Path(args.plan), args.sha256, opener=opener)


if __name__ == "__main__":
    sys.exit(main())
