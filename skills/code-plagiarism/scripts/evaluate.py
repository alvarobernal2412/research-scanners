"""Evaluation harness for the acceptance cases C1-C10 (maintainer tool, public fixtures only).

This is NOT the user-facing approval gate. In replay mode (the default) the harness approves each plan by
passing its own computed hash and answers every request from the recorded responses in
`tests/fixtures/responses/`, so it never touches the network. With `--live` it only writes all plans to
`--plans-dir` and prints their previews plus ONE combined sha256 over the sorted plan hashes; nothing is sent.
`--live --approve <combined sha256>` re-reads those plans, checks that the combined hash is the one you
approved, and then runs them against the real services (the fixtures are public code). `--record DIR` stores
the Stack Exchange and ClearlyDefined responses so the replay set can be refreshed.

    python -I evaluate.py <cases.json> [--tier strict|fingerprint|open|all]
    python -I evaluate.py <cases.json> --live --plans-dir D [--tier ...]
    python -I evaluate.py <cases.json> --live --plans-dir D --approve <combined sha256> [--record DIR]

Scoring (per case, per tier): a labelled flag is a true positive only when a flagged finding has a candidate
with the expected origin, the expected licence evidence and the expected attribution status (and match type
when the label has one). A flagged finding with the wrong source is a false positive and a false negative
(`fp+fn`). A labelled flag that was not found counts as a false negative only if the tier can reach it
(`reachable_tiers`); otherwise it is a `limitation`. A case labelled no-flag is `tn` unless something is flagged
(`fp`); informational findings (for example attribution present) do not count as predictions.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approval  # noqa: E402
import deps  # noqa: E402
import net  # noqa: E402
import report  # noqa: E402
import scanoss_backend as sb  # noqa: E402
import stackexchange as se  # noqa: E402
from approval import ApprovalError, render_preview, write_plan  # noqa: E402
from inventory import InventoryError  # noqa: E402
from model import write_results  # noqa: E402
from replay import RecordingOpener, ReplayOpener, ScanossReplayRunner  # noqa: E402

TIERS = ("strict", "fingerprint", "open")
OPEN_MAX_REQUESTS = 60


class EvalError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


# ---- labels and scoring ---------------------------------------------------------------------------------

def load_cases(path: Path) -> list:
    try:
        cases = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise EvalError("cases_invalid", "cannot read the cases file") from None
    if not isinstance(cases, list) or not cases:
        raise EvalError("cases_invalid", "the cases file must be a non-empty list")
    for c in cases:
        if not isinstance(c, dict) or not {"id", "root", "include", "labels", "reachable_tiers"} <= set(c):
            raise EvalError("cases_invalid", "a case is missing fields")
    return cases


def _so_id(url: str):
    m = re.search(r"stackoverflow\.com/(?:a|questions/\d+/[^/#?]+|revisions)/(\d+)", url or "")
    if m:
        return int(m.group(1))
    m = re.search(r"#(\d+)$", url or "")
    return int(m.group(1)) if m and "stackoverflow.com" in url else None


def _repo(url: str):
    m = re.search(r"github\.com/([^/\s#?]+)/([^/\s#?]+)", url or "", re.I)
    return (m.group(1).lower(), re.sub(r"\.git$", "", m.group(2).lower())) if m else None


def origin_matches(expected: str, candidate_url: str) -> bool:
    """Stack Overflow by post id; GitHub by owner/repo (case-insensitive); anything else by exact URL."""
    if not expected or not candidate_url:
        return False
    sid = _so_id(expected)
    if sid is not None:
        return sid == _so_id(candidate_url)
    repo = _repo(expected)
    if repo is not None:
        return repo == _repo(candidate_url)
    return expected.rstrip("/") == candidate_url.rstrip("/")


def _base(spdx) -> str:
    s = (spdx or "").strip().lower()
    for suffix in ("-or-later", "-only", "+"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    return s


def _license_ok(label, cand: dict) -> bool:
    if label is None:
        return True
    lics = cand.get("licenses") or []
    if label == "NONE":
        return bool(lics) and all(l.get("status") == "none_found" for l in lics)
    return any(_base(l.get("spdx")) == _base(label) for l in lics)


def _finding_ok(f: dict, labels: dict) -> bool:
    if labels.get("attribution") is not None and f["attribution"]["status"] != labels["attribution"]:
        return False
    if labels.get("match_type") is not None and f["match_type"] != labels["match_type"]:
        return False
    return any(origin_matches(labels["origin"], c["origin_url"]) and _license_ok(labels.get("license"), c)
               for c in f["candidates"])


def score_case(case: dict, rep: dict, tier: str) -> dict:
    labels = case["labels"]
    reach = case["reachable_tiers"]
    reachable = bool(reach) if tier == "all" else tier in reach
    findings = rep.get("findings", [])
    flagged = [f for f in findings if f["flag"]]
    if labels["flag"]:
        if any(_finding_ok(f, labels) for f in flagged):
            outcome = "tp"
        elif flagged:
            outcome = "fp+fn"
        elif reachable:
            outcome = "fn"
        else:
            outcome = "limitation"
    else:
        outcome = "fp" if flagged else "tn"
    return {
        "id": case["id"], "tier": tier, "outcome": outcome, "expected_flag": labels["flag"], "reachable": reachable,
        "findings": [{"file": f["file"], "lines": f["lines"], "match_type": f["match_type"], "flag": f["flag"],
                      "flag_reasons": f["flag_reasons"], "attribution": f["attribution"]["status"],
                      "candidates": [c["origin_url"] for c in f["candidates"]]} for f in findings],
    }


def tally(per_case: list) -> dict:
    count = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    limitations = 0
    for c in per_case:
        o = c["outcome"]
        if o == "fp+fn":
            count["fp"] += 1
            count["fn"] += 1
        elif o == "limitation":
            limitations += 1
        else:
            count[o] += 1
    pred, real = count["tp"] + count["fp"], count["tp"] + count["fn"]
    count["limitations"] = limitations
    count["precision"] = round(count["tp"] / pred, 3) if pred else None
    count["recall"] = round(count["tp"] / real, 3) if real else None
    return count


# ---- replay machinery -------------------------------------------------------------------------------------

class FingerprintReplayRunner:
    """Stand-in for scanoss-py: `--version`, an offline `fingerprint` of the staged files, and `scan` answered
    from the recorded results keyed by file MD5."""

    def __init__(self, by_md5_path):
        self.scan = ScanossReplayRunner(by_md5_path)
        self.calls = []

    def __call__(self, argv, env, cwd):
        self.calls.append(list(argv))
        if len(argv) > 1 and argv[1] == "--version":
            return 0, "Version: 1.54.2\n", ""
        if len(argv) > 2 and argv[1] == "fingerprint":
            stage_dir, out = Path(argv[2]), Path(argv[argv.index("-o") + 1])
            lines = []
            for p in sorted(stage_dir.iterdir()):
                data = p.read_bytes()
                flipped = data.replace(b"\r\n", b"\n") if b"\r\n" in data else data.replace(b"\n", b"\r\n")
                lines.append(f"file={hashlib.md5(data).hexdigest()},{len(data)},{p.name}")
                lines.append(f"fh2={hashlib.md5(flipped).hexdigest()}")
                for i, ln in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if i % 3 == 0 and ln.strip():
                        lines.append(f"{i}={zlib.crc32(ln.encode('utf-8')):08x}")
            out.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
            return 0, "", ""
        return self.scan(argv, env, cwd)


def _materialise(case: dict, fixtures: Path, dest: Path) -> Path:
    src = fixtures / case["root"]
    if not src.is_dir():
        raise EvalError("cases_invalid", f"missing fixture directory for {case['id']}")
    shutil.copytree(src, dest)
    for rel, text in (case.get("extra") or {}).items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return dest


def _plan_path(directory: Path, name: str, plan: dict) -> Path:
    path = directory / f"{name}.json"
    write_plan(plan, path)
    return path


def _split_open(res: dict, case_id: str, root_name: str) -> dict:
    """The combined open-tier results, reduced to one case (paths relative to the case, its own root name)."""
    out = copy.deepcopy(res)
    out["root"] = root_name
    prefix = f"{case_id}/"
    out["matches"] = [dict(m, rel=m["rel"][len(prefix):]) for m in res["matches"] if m["rel"].startswith(prefix)]
    out["dropped"] = [dict(d, rel=d["rel"][len(prefix):]) if str(d.get("rel", "")).startswith(prefix) else d
                      for d in res["dropped"] if str(d.get("rel", "")).startswith(prefix)]
    return out


def _case_includes(case: dict, prefix: str = "") -> list:
    return [prefix + i for i in case["include"]]


class _Work:
    """Temporary copies of the fixtures: one project per case, one combined project for the open tier."""

    def __init__(self, cases: list, fixtures: Path, base: Path):
        self.base = base
        self.case_root = {c["id"]: _materialise(c, fixtures, base / "cases" / c["id"]) for c in cases}
        self.combined = base / "combined"
        for c in cases:
            _materialise(c, fixtures, self.combined / c["id"])
        (base / "plans").mkdir()
        (base / "results").mkdir()


def _record_error(errors: list, tier: str, case_id, err: dict):
    entry = {"tier": tier, "case": case_id, "code": err.get("code"), "detail": err.get("detail")}
    if entry not in errors:
        errors.append(entry)


def _run_tier(tier: str, cases: list, work: _Work, responses: Path, errors: list, replay: dict):
    """Results file per case for one tier (replay mode). Returns {case id: results path}."""
    out: dict = {}
    if tier == "strict":
        for c in cases:
            root = work.case_root[c["id"]]
            plan = deps.make_plan(root)
            pp = _plan_path(work.base / "plans", f"strict-{c['id']}", plan)
            res = deps.run(pp, plan["sha256"], opener=ReplayOpener(responses), sleep=lambda s: None)
            out[c["id"]] = _store(work, f"strict-{c['id']}", res)
    elif tier == "fingerprint":
        runner = FingerprintReplayRunner(replay["by_md5"])
        for c in cases:
            root = work.case_root[c["id"]]
            plan = sb.make_plan(root, c["include"], runner=runner)
            pp = _plan_path(work.base / "plans", f"fingerprint-{c['id']}", plan)
            res = sb.run(pp, plan["sha256"], runner=runner)
            out[c["id"]] = _store(work, f"fingerprint-{c['id']}", res)
    else:
        includes = [i for c in cases for i in _case_includes(c, c["id"] + "/")]
        plan = se.make_plan(work.combined, includes, max_requests=OPEN_MAX_REQUESTS)
        pp = _plan_path(work.base / "plans", "open", plan)
        res = se.run(pp, plan["sha256"], work.combined, opener=ReplayOpener(responses), sleep=lambda s: None)
        for c in cases:
            out[c["id"]] = _store(work, f"open-{c['id']}", _split_open(res, c["id"], c["id"]))
    return out


def _store(work: _Work, name: str, res: dict) -> Path:
    path = work.base / "results" / f"{name}.json"
    write_results(path, res)
    return path


def _score_all(cases: list, work: _Work, tier: str, per_tier: dict, errors: list) -> list:
    per_case = []
    for c in cases:
        paths = [per_tier[t][c["id"]] for t in per_tier]
        try:
            rep = report.build_report(work.case_root[c["id"]], paths, use_scancode=False)
        except (report.ReportError, InventoryError) as exc:
            _record_error(errors, tier, c["id"], {"code": exc.code, "detail": getattr(exc, "detail", str(exc))})
            per_case.append({"id": c["id"], "tier": tier, "outcome": "error", "expected_flag": c["labels"]["flag"],
                             "reachable": False, "findings": []})
            continue
        for e in rep["coverage"]["errors"]:
            _record_error(errors, tier, c["id"], e)
        per_case.append(score_case(c, rep, tier))
    return per_case


def evaluate(cases_file: Path, tier: str = "all", responses_dir: Path | None = None) -> dict:
    cases_file = Path(cases_file)
    cases = load_cases(cases_file)
    fixtures = cases_file.parent
    responses = Path(responses_dir) if responses_dir else fixtures / "responses"
    if tier not in TIERS + ("all",):
        raise EvalError("usage", "tier must be strict, fingerprint, open or all")
    tiers = TIERS if tier == "all" else (tier,)
    replay = {"by_md5": responses / "scanoss_by_md5.json"}
    errors: list = []
    with tempfile.TemporaryDirectory(prefix="code-plag-eval-") as tmp:
        work = _Work(cases, fixtures, Path(tmp))
        per_tier = {t: _run_tier(t, cases, work, responses, errors, replay) for t in tiers}
        per_case = _score_all(cases, work, tier, per_tier, errors)
    result = {"tier": tier, **tally(per_case), "per_case": per_case,
              "known_limitations": [{"id": c["id"], "note": next(x["note"] for x in cases if x["id"] == c["id"])}
                                    for c in per_case if c["outcome"] == "limitation"],
              "errors": errors}
    return result


# ---- live mode -----------------------------------------------------------------------------------------------

def combined_hash(plan_hashes: list) -> str:
    return hashlib.sha256("\n".join(sorted(plan_hashes)).encode("utf-8")).hexdigest()


def _live_plans(cases: list, work: _Work, tiers, runner=None) -> tuple:
    """Build every plan with the real tools (nothing is sent). Returns (plans {name: plan}, skipped {tier: why})."""
    plans: dict = {}
    skipped: dict = {}
    for t in tiers:
        try:
            if t == "strict":
                for c in cases:
                    plans[f"strict-{c['id']}"] = deps.make_plan(work.case_root[c["id"]])
            elif t == "fingerprint":
                for c in cases:
                    plans[f"fingerprint-{c['id']}"] = sb.make_plan(work.case_root[c["id"]], c["include"],
                                                                   runner=runner)
            else:
                includes = [i for c in cases for i in _case_includes(c, c["id"] + "/")]
                plans["open"] = se.make_plan(work.combined, includes, max_requests=OPEN_MAX_REQUESTS)
        except (sb.ScanossError, se.StackExchangeError, ApprovalError, InventoryError) as exc:
            skipped[t] = getattr(exc, "code", type(exc).__name__)
            for name in [n for n in plans if n.startswith(t)]:
                del plans[name]
    return plans, skipped


def plan_live(cases_file: Path, plans_dir: Path, tier: str = "all", runner=None) -> dict:
    cases_file = Path(cases_file)
    cases = load_cases(cases_file)
    tiers = TIERS if tier == "all" else (tier,)
    plans_dir = Path(plans_dir)
    plans_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="code-plag-eval-") as tmp:
        work = _Work(cases, cases_file.parent, Path(tmp))
        plans, skipped = _live_plans(cases, work, tiers, runner)
    listing = []
    for name in sorted(plans):
        path = plans_dir / f"{name}.json"
        if path.exists():
            path.unlink()
        write_plan(plans[name], path, exclusive=True)
        listing.append({"name": name, "path": str(path.resolve()), "sha256": plans[name]["sha256"],
                        "requests": len(plans[name]["requests"]), "preview": render_preview(plans[name])})
    return {"live": True, "tier": tier, "plans": listing, "skipped_tiers": skipped,
            "combined_sha256": combined_hash([p["sha256"] for p in listing]), "sent": False}


def run_live(cases_file: Path, plans_dir: Path, approve: str, tier: str = "all", record: Path | None = None,
             responses_dir: Path | None = None, runner=None) -> dict:
    cases_file = Path(cases_file)
    cases = load_cases(cases_file)
    plans_dir = Path(plans_dir)
    tiers = TIERS if tier == "all" else (tier,)
    names = sorted(p.stem for p in plans_dir.glob("*.json") if p.stem.split("-")[0] in tiers or p.stem in tiers)
    if not names:
        raise EvalError("no_plans", "no plans found in --plans-dir; run --live without --approve first")
    plans = {n: approval.read_plan(plans_dir / f"{n}.json") for n in names}
    for n, p in plans.items():
        if p.get("sha256") != approval.plan_hash(p):
            raise ApprovalError("plan_hash_mismatch", f"plan {n} was edited")
    if combined_hash([p["sha256"] for p in plans.values()]) != approve:
        raise EvalError("approval_mismatch", "the approved combined sha256 does not match the plans on disk")
    errors: list = []
    with tempfile.TemporaryDirectory(prefix="code-plag-eval-") as tmp:
        work = _Work(cases, cases_file.parent, Path(tmp))
        per_tier: dict = {}
        for t in tiers:
            mine = [n for n in names if n == t or n.startswith(t + "-")]
            if not mine:
                continue
            out: dict = {}
            if t == "open":
                opener = RecordingOpener(net.build_default_opener(), Path(record)) if record else None
                res = se.run(plans_dir / "open.json", plans["open"]["sha256"], work.combined, opener=opener)
                for c in cases:
                    out[c["id"]] = _store(work, f"open-{c['id']}", _split_open(res, c["id"], c["id"]))
            else:
                for c in cases:
                    n = f"{t}-{c['id']}"
                    if n not in plans:
                        continue
                    if t == "strict":
                        opener = RecordingOpener(net.build_default_opener(), Path(record)) if record else None
                        res = deps.run(plans_dir / f"{n}.json", plans[n]["sha256"], opener=opener)
                    else:
                        res = sb.run(plans_dir / f"{n}.json", plans[n]["sha256"], runner=runner)
                    out[c["id"]] = _store(work, n, res)
            per_tier[t] = out
        per_case = []
        for c in cases:
            paths = [per_tier[t][c["id"]] for t in per_tier if c["id"] in per_tier[t]]
            try:
                rep = report.build_report(work.case_root[c["id"]], paths, use_scancode=False)
            except (report.ReportError, InventoryError) as exc:
                _record_error(errors, tier, c["id"], {"code": exc.code, "detail": getattr(exc, "detail", "")})
                continue
            for e in rep["coverage"]["errors"]:
                _record_error(errors, tier, c["id"], e)
            per_case.append(score_case(c, rep, tier))
    live = {"tier": tier, **tally(per_case), "per_case": per_case, "errors": errors}
    try:
        base = evaluate(cases_file, tier, responses_dir)
        old = {c["id"]: c["outcome"] for c in base["per_case"]}
        drift = [{"id": c["id"], "replay": old.get(c["id"]), "live": c["outcome"]}
                 for c in per_case if old.get(c["id"]) != c["outcome"]]
        replay = {k: base[k] for k in ("tp", "fp", "fn", "tn", "limitations", "precision", "recall")}
    except (EvalError, ApprovalError, OSError):
        drift, replay = None, None
    return {"live": True, "approved": approve, "result": live, "replay": replay, "drift": drift}


# ---- CLI ---------------------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": "usage", "detail": message}))
        raise SystemExit(2)


def main(argv: list, runner=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = _Parser(prog="evaluate.py")
    ap.add_argument("cases")
    ap.add_argument("--tier", default="all", choices=list(TIERS) + ["all"])
    ap.add_argument("--responses")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--plans-dir")
    ap.add_argument("--approve")
    ap.add_argument("--record")
    args = ap.parse_args(argv)
    try:
        if args.live:
            if not args.plans_dir:
                raise EvalError("usage", "--live needs --plans-dir")
            if args.approve:
                out = run_live(Path(args.cases), Path(args.plans_dir), args.approve, args.tier,
                               Path(args.record) if args.record else None,
                               Path(args.responses) if args.responses else None, runner=runner)
            else:
                out = plan_live(Path(args.cases), Path(args.plans_dir), args.tier, runner=runner)
        else:
            out = evaluate(Path(args.cases), args.tier, Path(args.responses) if args.responses else None)
        json.dumps(out, allow_nan=False)
    except (EvalError, ApprovalError, InventoryError) as exc:
        print(json.dumps({"error": exc.code, "detail": getattr(exc, "detail", None) or str(exc)}))
        return 2
    except (OSError, ValueError):
        print(json.dumps({"error": "output_error", "detail": "file system or encoding error"}))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
