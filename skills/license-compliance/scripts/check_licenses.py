"""Check a project's dependency licenses against the project's own license.

Usage: python -I check_licenses.py <project dir> [--offline] [--include-dev] [--project-license SPDX]

Prints one JSON report. Report-only: it never edits files and gives no legal conclusion.
Only registry package names and versions are sent to PyPI / npm (see lookup_registry.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compat  # noqa: E402
import deps as deps_mod  # noqa: E402
import licenses  # noqa: E402
import lookup_registry  # noqa: E402
import recommend as recommend_mod  # noqa: E402

DISCLAIMER = ("Compatibility is judged from commonly published interpretations of the licenses. It is not legal "
              "advice and depends on how the code is combined and distributed. Registry license data is "
              "self-declared by package authors and can be wrong or missing.")
LOOKUP_REASONS = {
    "unregistered_source": "unregistered_source",
    "not_found": "lookup_failed",
    "lookup_failed": "lookup_failed",
    "offline_no_local_data": "lookup_failed",
}


def _judge_dep(d: dict, project_license: str | None) -> dict:
    if d["scope"] == "dev":
        return {"status": "not_judged", "reason": "dev_dependency", "conditions": [], "why": (
            "development-only dependencies are not distributed with the project")}
    if d["license"] is None and d["lookup_status"] in LOOKUP_REASONS:
        reason = LOOKUP_REASONS[d["lookup_status"]]
        why = {"unregistered_source": "installed from git, a path or a URL; not sent to any registry",
               "not_found": "the registry has no record of this package/version",
               "lookup_failed": "the registry lookup failed",
               "offline_no_local_data": "offline mode and no local metadata"}[d["lookup_status"]]
        return {"status": "needs_review", "reason": reason, "conditions": [], "why": why}
    res = compat.judge(d["license"], project_license)
    if res["status"] == "compatible" and d.get("license_confidence") == "low" and " OR " in (d["license"] or ""):
        as_and = compat.judge(d["license"].replace(" OR ", " AND "), project_license)
        if as_and["status"] != "compatible":
            return {"status": "needs_review", "reason": "dual_license_ambiguous", "conditions": res["conditions"],
                    "why": "several license classifiers were declared; compatible only if they are alternatives"}
    return {k: res[k] for k in ("status", "reason", "conditions", "why")}


def build_report(root: Path, offline: bool = False, include_dev: bool = False, project_license: str | None = None,
                 opener=None) -> dict:
    collected = deps_mod.collect(root)
    if not collected["manifests"]:
        return {"error": "no_manifests_found"}
    proj = licenses.detect_project_license(root)
    if project_license:
        norm = licenses.normalize(project_license)
        proj = {"spdx": norm, "evidence": [{"source": "--project-license", "spdx": norm, "kind": "override"}],
                "notes": proj["notes"]}
    looked = lookup_registry.resolve_all(collected["dependencies"], root, offline=offline,
                                         include_dev=include_dev, opener=opener)
    ds = looked["dependencies"]

    runtime = [{"name": d["name"], "version": d["version"], "license": d["license"]}
               for d in ds if d["scope"] == "runtime"]
    own = proj["spdx"]
    rec = None
    judged_against, basis = own, "project"
    verdicts = [_judge_dep(d, own) for d in ds]
    conflicts_with_own = any(v["status"] == "conflict" for v in verdicts)
    if own is None or conflicts_with_own:
        rec = recommend_mod.recommend(runtime)
        if own is None:
            judged_against = rec["recommended"]
            basis = "recommended" if judged_against else None
            verdicts = [_judge_dep(d, judged_against) for d in ds]

    items = []
    for d, v in zip(ds, verdicts):
        items.append({
            "name": d["name"], "version": d["version"], "ecosystem": d["ecosystem"], "scope": d["scope"],
            "direct": d["direct"], "license": d["license"], "license_category": (
                compat.category(licenses.parse_expression(d["license"])[1])
                if d["license"] and licenses.parse_expression(d["license"]) and licenses.parse_expression(d["license"])[0] == "id"
                else None),
            "license_evidence": d["license_evidence"], "license_confidence": d["license_confidence"],
            "license_note": d["license_note"], "lookup_status": d["lookup_status"], "verdict": v})

    counts: dict[str, int] = {}
    for it in items:
        counts[it["verdict"]["status"]] = counts.get(it["verdict"]["status"], 0) + 1
    conflicts = [i for i in items if i["verdict"]["status"] == "conflict"]
    not_usable = [i for i in items if i["verdict"]["status"] == "conflict" or (
        i["verdict"]["status"] == "needs_review" and i["verdict"]["reason"] in
        ("no_license", "unknown_license", "non_free_license"))]

    coverage = list(collected["notes"])
    coverage += [f"license sources disagree or are unclear: {n}" for n in proj["notes"]]
    if own is None:
        coverage.append("The project has no recognisable license. Dependencies are judged against the "
                        "recommended license; add the license file and re-run.")
    failed = [i["name"] for i in items if i["scope"] == "runtime" and i["lookup_status"] in
              ("lookup_failed", "not_found")]
    if failed:
        coverage.append("License lookup failed for: " + ", ".join(failed) + ". They are not treated as compatible.")
    if offline:
        coverage.append("Offline mode: only lockfile and locally installed metadata were used.")
    private = [i["name"] for i in items if i["lookup_status"] == "unregistered_source"]
    if private:
        coverage.append("Not sent to any registry (git/path/url source): " + ", ".join(private))
    dev_count = sum(1 for i in items if i["scope"] == "dev")
    if dev_count:
        coverage.append(f"{dev_count} development-only dependencies were listed but not judged (not distributed).")
    coverage.append("Code vendored or bundled inside dependencies was not inspected.")

    return {
        "project": {"root": str(root), "license": own, "evidence": proj["evidence"], "notes": proj["notes"]},
        "judged_against": {"license": judged_against, "basis": basis},
        "summary": {"dependencies": len(items), "verdicts": counts,
                    "conflicts": len(conflicts), "not_usable_as_is": len(not_usable),
                    "result": ("conflicts found" if conflicts else "no conflicts found among the checked dependencies")},
        "manifests": [m["file"] for m in collected["manifests"]],
        "dependencies": items,
        "conflicts": [{"name": i["name"], "version": i["version"], "license": i["license"], "why": i["verdict"]["why"]}
                      for i in conflicts],
        "not_usable_as_is": [{"name": i["name"], "version": i["version"], "license": i["license"],
                              "reason": i["verdict"]["reason"], "why": i["verdict"]["why"]} for i in not_usable],
        "needs_review": [{"name": i["name"], "version": i["version"], "license": i["license"],
                          "reason": i["verdict"]["reason"], "why": i["verdict"]["why"]}
                         for i in items if i["verdict"]["status"] == "needs_review"],
        "recommendation": rec,
        "coverage": coverage,
        "requests_log": looked["requests_log"],
        "disclaimer": DISCLAIMER,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check dependency licenses against the project license.")
    ap.add_argument("root")
    ap.add_argument("--offline", action="store_true", help="make no network requests")
    ap.add_argument("--include-dev", action="store_true", help="also look up development-only dependencies")
    ap.add_argument("--project-license", help="SPDX id/expression to use instead of the detected license")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = Path(args.root)
    if not root.is_dir():
        print(json.dumps({"error": "not_a_directory"}))
        return 2
    report = build_report(root.resolve(), args.offline, args.include_dev, args.project_license)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if "error" in report else 0


if __name__ == "__main__":
    sys.exit(main())
