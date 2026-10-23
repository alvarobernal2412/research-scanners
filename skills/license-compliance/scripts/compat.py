"""License compatibility rules (data/compat.json). Local only, no network. Not legal advice."""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import licenses  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data" / "compat.json"
RANK = {"conflict": 0, "needs_review": 1, "compatible": 2}  # lower = worse


@lru_cache(maxsize=1)
def table() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _expand(entries, groups: dict) -> set[str]:
    out: set[str] = set()
    for e in entries:
        out |= set(groups[e[1:]]) if e.startswith("@") else {e}
    return out


def category(spdx_id: str) -> str:
    for cat, ids in table()["categories"].items():
        if spdx_id in ids:
            return cat
    return "unknown"


def _result(status, conditions=None, reason=None, why=None, license_id=None):
    return {"status": status, "conditions": list(conditions or []), "reason": reason, "why": why,
            "license": license_id}


def judge_id(dep_id: str, project_id: str | None, exception: str | None = None) -> dict:
    """Is a dependency under license dep_id usable inside a project licensed project_id?"""
    t = table()
    if exception:
        eff = t["exception_effect"].get(exception)
        if eff is None:
            return _result("needs_review", reason="exception_clause",
                           why=f"license exception {exception} is not in the rule table", license_id=dep_id)
        return _result("compatible", eff["conditions"], license_id=dep_id)
    if dep_id in t["needs_review"]:
        return _result("needs_review", reason="non_free_license", why=t["needs_review"][dep_id], license_id=dep_id)
    rule = t["rules"].get(dep_id)
    if rule is None and dep_id in t["categories"]["permissive"]:
        rule = t["rules"]["permissive"]
    if rule is None:
        return _result("needs_review", reason="unknown_license",
                       why=f"no compatibility rule for {dep_id}", license_id=dep_id)
    if project_id is None:
        return _result("needs_review", reason="project_has_no_license", license_id=dep_id)
    groups = t["groups"]
    allowed = rule["allowed_projects"]
    blocked = _expand(rule.get("not_allowed_projects", []), groups)
    ok = (allowed == "any" or project_id in _expand(allowed, groups)) and project_id not in blocked
    if ok:
        return _result("compatible", rule.get("conditions"), license_id=dep_id)
    return _result("conflict", reason="incompatible_license", why=rule.get("why"), license_id=dep_id)


def judge_node(node, project_id: str | None) -> dict:
    kind = node[0]
    if kind == "id":
        return judge_id(node[1], project_id, node[2])
    results = [judge_node(n, project_id) for n in node[1]]
    if kind == "and":
        worst = min(results, key=lambda r: RANK[r["status"]])
        if worst["status"] == "compatible":
            return {**worst, "conditions": sorted({c for r in results for c in r["conditions"]})}
        return worst
    best = max(results, key=lambda r: RANK[r["status"]])  # OR: the dependency's author lets you choose
    if best["status"] == "compatible":
        best = {**best, "conditions": best["conditions"] + ["dual-licensed: use the compatible option"]}
    return best


def _project_ids(pnode) -> list[str]:
    if pnode[0] == "id":
        return [pnode[1]]
    return [i for n in pnode[1] for i in _project_ids(n)]


def judge(dep_expression: str | None, project_expression: str | None) -> dict:
    """Judge a dependency's license expression against the project's license expression.

    A dual-licensed project (A OR B) offers each option downstream, so the dependency must be
    usable under every option.
    """
    if dep_expression is None:
        return _result("needs_review", reason="no_license",
                       why="no license found: all rights reserved by default, never assume permissive")
    node = licenses.parse_expression(dep_expression)
    if node is None:
        return _result("needs_review", reason="unknown_license",
                       why=f"could not interpret license '{dep_expression}'")
    if project_expression is None:
        return judge_node(node, None)
    pnode = licenses.parse_expression(project_expression)
    if pnode is None:
        return _result("needs_review", reason="project_license_unrecognised")
    results = [judge_node(node, pid) for pid in _project_ids(pnode)]
    return min(results, key=lambda r: RANK[r["status"]])
