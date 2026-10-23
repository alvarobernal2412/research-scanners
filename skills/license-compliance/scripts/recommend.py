"""Recommend a project license from the runtime dependency set. Local only. Not legal advice."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compat  # noqa: E402

UNRESOLVED_REASONS = ("no_license", "unknown_license", "non_free_license", "exception_clause")


def recommend(runtime_deps: list[dict]) -> dict:
    """runtime_deps: [{name, version, license (SPDX expression or None)}].

    Returns usable candidate licenses (best first), the ruled-out ones with the dependencies that
    block them, a recommendation, and the dependencies whose license could not be interpreted
    (they are not judged, so the recommendation is provisional while any exist).
    """
    t = compat.table()
    candidates = t["candidates"]
    unresolved = []
    for d in runtime_deps:
        res = compat.judge(d["license"], candidates[0])
        if res["status"] == "needs_review" and res["reason"] in UNRESOLVED_REASONS:
            unresolved.append(d)
    skip = {id(d) for d in unresolved}
    usable, ruled_out = [], []
    for cand in candidates:
        blockers = []
        for d in runtime_deps:
            if id(d) in skip:
                continue
            res = compat.judge(d["license"], cand)
            if res["status"] == "conflict":
                blockers.append({"dependency": d["name"], "license": d["license"], "why": res["why"]})
        entry = {"license": cand, "note": t["candidate_notes"].get(cand)}
        if blockers:
            ruled_out.append({**entry, "blocked_by": blockers})
        else:
            usable.append(entry)
    rec = usable[0] if usable else None
    caveats = []
    if rec is None:
        caveats.append("No candidate license satisfies every runtime dependency; some dependencies must be "
                       "replaced or obtained under different terms.")
    if rec and rec["license"] == "Apache-2.0":
        caveats.append("Apache-2.0 is preferred over MIT/BSD when everything is permissive because of its "
                       "explicit patent grant (a default choice, not a requirement).")
    if unresolved:
        caveats.append("Dependencies with no or unknown license were not considered; the recommendation is "
                       "provisional until they are resolved.")
    return {
        "recommended": rec["license"] if rec else None,
        "usable": usable,
        "not_usable": ruled_out,
        "unresolved_dependencies": [{"dependency": d["name"], "license": d["license"]} for d in unresolved],
        "caveats": caveats,
    }
