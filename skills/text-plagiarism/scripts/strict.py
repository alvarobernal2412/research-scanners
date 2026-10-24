"""Strict tier: plan/run rounds that send only public identifiers and reference-list strings.

    python -I strict.py plan --workdir W                       writes the next round's plan, prints its sha256
    python -I strict.py run  --workdir W --plan P --sha256 H   sends only the requests in that approved plan

Rounds: resolve (Crossref, reference strings) -> locate (OpenAlex, DOIs) -> zenodo (records) -> download
(open-access full texts). `run` never builds a request itself: it iterates the approved plan.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquire  # noqa: E402
import refs as refs_mod  # noqa: E402
from approval import ApprovalError, Gate, build_plan, load_approved_plan, preview, write_plan  # noqa: E402
from common import configure_stdout, emit, fail  # noqa: E402
from findings import load_state  # noqa: E402
from net import Cache, RequestLog, cache_root, send  # noqa: E402
from prepare import load_doc  # noqa: E402

ROUNDS = ("resolve", "locate", "zenodo", "download")


def _save_state(workdir: Path, state: dict) -> None:
    (workdir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _entries(state: dict, workdir: Path):
    if state.get("references") is None:
        state["references"] = [asdict(r) for r in load_doc(workdir)[2]]
    return [refs_mod.RefEntry(**r) for r in state["references"]]


def source_key(doi: str | None, arxiv: str | None) -> str:
    return doi.lower() if doi else f"arxiv:{arxiv.split('v')[0]}"


def build_sources(entries: list, existing: list[acquire.SourceInfo]) -> list[acquire.SourceInfo]:
    by_key = {s.key: s for s in existing}
    for i, e in enumerate(entries):
        doi = e.doi or e.resolved_doi
        if not doi and not e.arxiv:
            continue
        key = source_key(doi, e.arxiv)
        if key in by_key:
            continue
        info = acquire.SourceInfo(key=key, ref_n=e.n if e.n is not None else i + 1, doi=doi, title=e.title,
                                  arxiv=e.arxiv)
        if not doi and e.arxiv:        # an arXiv id alone: the PDF is the only candidate, no locate round
            url = f"https://arxiv.org/pdf/{e.arxiv.split('v')[0]}"
            info.candidates.append(url)
            info.cand_meta[url] = {"license": None, "version": None}
            info.is_oa = True
        by_key[key] = info
    return list(by_key.values())


def _load_infos(state: dict) -> list[acquire.SourceInfo]:
    return [acquire.SourceInfo(**d) for d in state["sources"]]


def _requests_for(round_name: str, entries, infos):
    if round_name == "resolve":
        return refs_mod.plan_resolution(entries)
    if round_name == "locate":
        return acquire.plan_locate(infos)
    if round_name == "zenodo":
        return acquire.plan_zenodo(infos)
    return acquire.plan_download(infos)


def cmd_plan(workdir: Path) -> int:
    if not (workdir / "doc.json").is_file():
        return fail("workdir_missing", str(workdir))
    doc, _, _ = load_doc(workdir)
    state = load_state(workdir)
    entries = _entries(state, workdir)
    infos = build_sources(entries, _load_infos(state))
    acquire.settle(infos)
    for round_name in ROUNDS:
        reqs = _requests_for(round_name, entries, infos)
        if not reqs:
            continue
        state["sources"] = [asdict(i) for i in infos]       # plan_locate may record Zenodo ids
        _save_state(workdir, state)
        plan = build_plan("strict", round_name, doc.sha256, reqs)
        n = len(list((workdir / "plans").glob("strict-*.json"))) + 1 if (workdir / "plans").is_dir() else 1
        path = workdir / "plans" / f"strict-{n:02d}-{round_name}.json"
        sha = write_plan(plan, path)
        emit({"done": False, "round": round_name, "plan": str(path), "sha256": sha, "preview": preview(plan)})
        return 0
    state["sources"] = [asdict(i) for i in infos]
    _save_state(workdir, state)
    emit({"done": True})
    return 0


def cmd_run(workdir: Path, plan_path: Path, sha256: str, opener=None) -> int:
    if not (workdir / "doc.json").is_file():
        return fail("workdir_missing", str(workdir))
    doc, _, _ = load_doc(workdir)
    try:
        plan = load_approved_plan(plan_path, sha256, doc.sha256)
    except ApprovalError as exc:
        return fail(exc.code, exc.detail or None)
    if plan["tier"] != "strict":
        return fail("plan_invalid", "not a strict-tier plan")
    gate = Gate(plan)
    state = load_state(workdir)
    entries = _entries(state, workdir)
    infos = build_sources(entries, _load_infos(state))
    by_key = {i.key: i for i in infos}
    sleep = time.sleep if opener is None else (lambda s: None)
    cache = Cache(cache_root())
    log = RequestLog(workdir / "requests.jsonl")
    round_name = plan["round"]
    responses: dict[str, object] = {}
    counts = Counter()
    errors = Counter()
    store = cache_root() / "sources"
    for req in gate.requests():
        resp = send(req, gate, tier="strict", round_name=round_name, opener=opener, cache=cache, log=log, sleep=sleep)
        if resp.from_cache:
            counts["cached"] += 1
        else:
            counts["sent"] += 1
        if resp.error:
            errors[resp.error] += 1
        elif resp.status == 200:
            counts["ok"] += 1
        else:
            errors[f"http_{resp.status}"] += 1
        subject = req.subject
        if round_name == "resolve":
            responses[subject] = resp
            continue
        if subject.startswith("source:"):
            info = by_key.get(subject[len("source:"):])
        elif subject.startswith("zenodo:"):
            info = by_key.get(subject[len("zenodo:"):].rsplit(":", 1)[0])
        else:
            info = None
        if info is None:
            continue
        if round_name == "locate":
            if resp.error or resp.status != 200:
                info.status, info.reason = "not_obtained", "backend_error"
            else:
                acquire.apply_locate(info, resp.body)
        elif round_name == "zenodo":
            zid = subject.rsplit(":", 1)[-1]
            if resp.error or resp.status != 200:
                info.zenodo_done.append(zid)
                info.reason = info.reason or "backend_error"
            else:
                acquire.apply_zenodo(info, resp.body, zid)
        else:
            acquire.apply_download(info, resp, store, url=req.url)
    if round_name == "resolve":
        refs_mod.apply_resolution(entries, responses)       # type: ignore[arg-type]
        state["references"] = [asdict(e) for e in entries]
        infos = build_sources(entries, infos)
    acquire.settle(infos)
    state["sources"] = [asdict(i) for i in infos]
    state.setdefault("rounds", []).append({"round": round_name, "plan": plan_path.name, "sha256": sha256,
                                           "sent": counts["sent"], "cached": counts["cached"]})
    _save_state(workdir, state)
    emit({"round": round_name, "sent": counts["sent"], "ok": counts["ok"], "cached": counts["cached"],
          "errors": dict(errors)})
    return 0


def main(argv: list[str] | None = None, opener=None) -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(prog="strict.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--workdir", required=True)
    r = sub.add_parser("run")
    r.add_argument("--workdir", required=True)
    r.add_argument("--plan", required=True)
    r.add_argument("--sha256", required=True)
    args = ap.parse_args(argv)
    workdir = Path(args.workdir)
    if not workdir.is_dir():
        return fail("workdir_missing", str(workdir))
    if args.cmd == "plan":
        return cmd_plan(workdir)
    return cmd_run(workdir, Path(args.plan), args.sha256, opener=opener)


if __name__ == "__main__":
    sys.exit(main())
