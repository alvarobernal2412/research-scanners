"""Plan/run approval gate: the privacy boundary of the skill.

Every networked step is two-phase. `plan` writes the exact outgoing requests to a file and prints its
sha256; `run` sends only requests that are in a plan whose hash matches the approved one. `net.send`
asks a `Gate` before any I/O, and there is no flag or environment variable that turns the check off.

Tiers: strict plans may carry only public identifiers (DOIs, arXiv ids, URLs, reference-list strings).
Open plans may also carry `user_text`: exact phrases the user saw in a preview and approved.
A plan never holds a secret value; it holds `{ENV_NAME}` placeholders that `net.send` renders at send time.

CLI:  python -I approval.py show <plan.json>
      python -I approval.py strike <plan.json> --ids r3,r7
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import configure_stdout, emit, fail  # noqa: E402

PLAN_VERSION = 1
CARRIES = {"public_id", "reference_string", "public_url", "user_text"}
STRICT_CARRIES = frozenset({"public_id", "reference_string", "public_url"})
SECRET_ENVS = frozenset({"OPENALEX_API_KEY", "CORE_API_KEY", "BRAVE_API_KEY"})
IDENTITY_NAMES = SECRET_ENVS | {"TEXT_PLAGIARISM_MAILTO"}
TIERS = ("strict", "open")
PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


@dataclass
class PlannedRequest:
    id: str
    service: str
    method: str
    url: str
    carries: str
    purpose: str
    subject: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    text_sent: str | None = None
    identity: str | None = None


class ApprovalError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class UnapprovedRequest(Exception):
    pass


def _placeholders(req: PlannedRequest) -> set[str]:
    found = set(PLACEHOLDER.findall(req.url))
    for v in req.headers.values():
        found.update(PLACEHOLDER.findall(v))
    if req.body:
        found.update(PLACEHOLDER.findall(req.body))
    return found


def _secret_values() -> list[str]:
    return [v for v in (os.environ.get(n) for n in sorted(SECRET_ENVS)) if v and len(v) >= 4]


def build_plan(tier: str, round_name: str, document_sha256: str, requests: list[PlannedRequest]) -> dict:
    if tier not in TIERS:
        raise ValueError(f"unknown tier: {tier}")
    seen: set[str] = set()
    entries = []
    for n, req in enumerate(requests, 1):
        rid = req.id or f"r{n:03d}"
        if rid in seen:
            raise ValueError(f"duplicate request id: {rid}")
        seen.add(rid)
        if req.carries not in CARRIES:
            raise ValueError(f"unknown carries: {req.carries}")
        if tier == "strict" and req.carries not in STRICT_CARRIES:
            raise ValueError("a strict plan cannot contain user_text")
        if req.method not in ("GET", "POST"):
            raise ValueError(f"unsupported method: {req.method}")
        if req.carries == "user_text" and not req.text_sent:
            raise ValueError("a user_text request must state the text it sends (text_sent)")
        if req.carries != "user_text" and req.text_sent is not None:
            raise ValueError("only user_text requests may carry text_sent")
        unknown = _placeholders(req) - SECRET_ENVS
        if unknown:
            raise ValueError(f"placeholder is not an allowed secret: {sorted(unknown)[0]}")
        if req.identity is not None and req.identity not in IDENTITY_NAMES:
            raise ValueError(f"unknown identity: {req.identity}")
        entry = asdict(req)
        entry["id"] = rid
        entries.append(entry)
    plan = {"version": PLAN_VERSION, "tier": tier, "round": round_name, "document_sha256": document_sha256,
            "requests": entries}
    text = json.dumps(plan, ensure_ascii=False)
    for secret in _secret_values():
        if secret in text:
            raise ValueError("a secret value appears in the plan; use {ENV_NAME} placeholders")
    return plan


def _dump(plan: dict) -> bytes:
    return (json.dumps(plan, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_plan(plan: dict, path: Path) -> str:
    data = _dump(plan)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


_REQ_KEYS = {"id", "service", "method", "url", "carries", "purpose"}


def _validate(plan) -> None:
    if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION or plan.get("tier") not in TIERS:
        raise ApprovalError("plan_invalid", "bad header")
    reqs = plan.get("requests")
    if not isinstance(reqs, list) or not isinstance(plan.get("document_sha256"), str):
        raise ApprovalError("plan_invalid", "bad requests")
    ids = set()
    for r in reqs:
        if not isinstance(r, dict) or not _REQ_KEYS <= set(r) or r["carries"] not in CARRIES:
            raise ApprovalError("plan_invalid", "bad request entry")
        if plan["tier"] == "strict" and r["carries"] not in STRICT_CARRIES:
            raise ApprovalError("plan_invalid", "user_text in a strict plan")
        if r["id"] in ids:
            raise ApprovalError("plan_invalid", "duplicate id")
        ids.add(r["id"])


def load_approved_plan(path: Path, sha256: str, document_sha256: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ApprovalError("plan_missing", path.name)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != (sha256 or "").strip().lower():
        raise ApprovalError("plan_hash_mismatch", "the plan file is not the one that was approved")
    try:
        plan = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApprovalError("plan_invalid", "not valid JSON") from None
    _validate(plan)
    if plan["document_sha256"] != document_sha256:
        raise ApprovalError("plan_document_mismatch", "the plan was made for another document")
    return plan


def _to_request(entry: dict) -> PlannedRequest:
    return PlannedRequest(
        id=entry["id"], service=entry["service"], method=entry["method"], url=entry["url"],
        carries=entry["carries"], purpose=entry["purpose"], subject=entry.get("subject", ""),
        headers=dict(entry.get("headers") or {}), body=entry.get("body"), text_sent=entry.get("text_sent"),
        identity=entry.get("identity"))


class Gate:
    """Only requests that equal an entry of the approved plan may be sent."""

    def __init__(self, plan: dict):
        self.plan = plan
        self._by_id = {e["id"]: json.dumps(e, sort_keys=True) for e in plan["requests"]}

    def requests(self) -> list[PlannedRequest]:
        """Fresh copies: changing one never changes what the plan approved."""
        return [_to_request(json.loads(self._by_id[e["id"]])) for e in self.plan["requests"]]

    def check(self, req: PlannedRequest) -> None:
        frozen = self._by_id.get(req.id)
        if frozen is None:
            raise UnapprovedRequest(f"{req.id}: not in the approved plan")
        approved = _to_request(json.loads(frozen))
        for name in ("method", "url", "headers", "body", "carries", "text_sent", "service"):
            if getattr(req, name) != getattr(approved, name):
                raise UnapprovedRequest(f"{req.id}: {name} differs from the approved plan")


def preview(plan: dict) -> dict:
    groups: dict[str, dict] = {}
    for e in plan["requests"]:
        g = groups.setdefault(e["service"], {"count": 0, "carries": set(), "identity": set(), "items": []})
        g["count"] += 1
        g["carries"].add(e["carries"])
        if e.get("identity"):
            g["identity"].add(e["identity"])
        g["items"].append({"id": e["id"], "purpose": e["purpose"],
                           "sends": e["text_sent"] if e.get("text_sent") is not None else e["url"]})
    out = {name: {"count": g["count"], "carries": sorted(g["carries"]), "identity": sorted(g["identity"]),
                  "items": g["items"]} for name, g in sorted(groups.items())}
    return {"tier": plan["tier"], "round": plan["round"], "total_requests": len(plan["requests"]),
            "services": out}


def _norm_id(raw: str) -> str:
    m = re.fullmatch(r"r?0*(\d+)", raw.strip(), re.I)
    return f"r{int(m.group(1)):03d}" if m else raw.strip()


def strike(path: Path, ids: list[str]) -> str:
    path = Path(path)
    plan = json.loads(path.read_bytes().decode("utf-8"))
    _validate(plan)
    wanted = {_norm_id(i) for i in ids if i.strip()}
    known = {e["id"] for e in plan["requests"]}
    if wanted - known:
        raise ApprovalError("unknown_ids", ",".join(sorted(wanted - known)))
    plan["requests"] = [e for e in plan["requests"] if e["id"] not in wanted]
    return write_plan(plan, path)


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(prog="approval.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("plan")
    t = sub.add_parser("strike")
    t.add_argument("plan")
    t.add_argument("--ids", required=True)
    args = ap.parse_args(argv)
    path = Path(args.plan)
    if not path.is_file():
        return fail("plan_missing", path.name)
    try:
        if args.cmd == "show":
            data = path.read_bytes()
            plan = json.loads(data.decode("utf-8"))
            _validate(plan)
            emit({"sha256": hashlib.sha256(data).hexdigest(), "preview": preview(plan)})
        else:
            before = json.loads(path.read_bytes().decode("utf-8"))
            new_hash = strike(path, args.ids.split(","))
            after = json.loads(path.read_bytes().decode("utf-8"))
            emit({"sha256": new_hash, "removed": len(before["requests"]) - len(after["requests"]),
                  "remaining": len(after["requests"])})
    except ApprovalError as exc:
        return fail(exc.code, exc.detail)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return fail("plan_invalid", "not valid JSON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
