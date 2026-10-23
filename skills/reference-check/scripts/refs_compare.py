"""Compare a cited reference with an index record and derive a verdict. Local only, no network.

Record shape: {doi, title, authors: [family names], year, venues: [str], type, url, source}.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

TITLE_OK = 0.90        # title similarity treated as the same title
TITLE_MIN = 0.75       # below this the record is a different work
CANDIDATE_TITLE = 0.90  # title-search candidates are accepted only above this


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"<[^>]+>", " ", s)  # markup in registry titles
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_similarity(a: str | None, b: str | None) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    shorter, longer = sorted((na, nb), key=len)
    if len(shorter) >= 15 and longer.startswith(shorter):  # cited without the subtitle
        return 0.95
    return SequenceMatcher(None, na, nb).ratio()


def _surname(s: str) -> str:
    return norm(s).split(" ")[-1] if norm(s) else ""


def authors_check(cited: list[str], record: list[str], et_al: bool) -> dict:
    """Returns {first_ok, overlap, note}. Cited names are family names (or 'Initials Family')."""
    c = [_surname(a) for a in cited if _surname(a)]
    r = [_surname(a) for a in record if _surname(a)]
    if not c or not r:
        return {"first_ok": None, "overlap": None}
    first_ok = c[0] == r[0] or c[0] in r[:3]
    shared = len(set(c) & set(r))
    denom = len(c) if (et_al or len(c) < len(r)) else len(r)
    return {"first_ok": first_ok, "overlap": shared / max(1, denom)}


def _initials(title: str) -> str:
    stop = {"of", "the", "on", "and", "in", "for", "a", "an", "to"}
    return "".join(w[0] for w in norm(title).split() if w not in stop)


def venue_check(cited: str | None, venues: list[str]) -> bool | None:
    """True/False, or None when nothing to compare."""
    if not cited or not venues:
        return None
    cn = norm(cited)
    cn_clean = re.sub(r"\b(proc|proceedings|in|of|the|conference|conf|journal|j|int|international|trans|"
                      r"transactions|on|ieee|acm)\b", " ", cn)
    cn_clean = re.sub(r"\s+", " ", cn_clean).strip()
    for v in venues:
        vn = norm(v)
        if not vn:
            continue
        if cn == vn or cn in vn or vn in cn:
            return True
        if SequenceMatcher(None, cn, vn).ratio() >= 0.6:
            return True
        ini = _initials(v)
        toks = set(cn.split())
        if len(ini) >= 3 and ini in toks:
            return True
        # abbreviated words: every cited token is a prefix of a record token, in order
        ctoks = cn_clean.split()
        vtoks = norm(v).split()
        if ctoks and all(any(vt.startswith(ct[:3]) for vt in vtoks) for ct in ctoks if len(ct) >= 2):
            return True
    return False


def compare(ref: dict, rec: dict) -> dict:
    """Compare a parsed reference with a record. Returns {verdict, differences, title_similarity}."""
    diffs = []
    sim = title_similarity(ref.get("title"), rec.get("title")) if ref.get("title") else None
    material = False
    if sim is not None and sim < TITLE_OK:
        diffs.append({"field": "title", "cited": ref["title"], "record": rec.get("title"),
                      "similarity": round(sim, 2)})
        if sim < TITLE_MIN:
            material = True
    a = authors_check(ref.get("authors") or [], rec.get("authors") or [], bool(ref.get("et_al")))
    if a["first_ok"] is False:
        diffs.append({"field": "authors", "cited": ref.get("authors"), "record": rec.get("authors")})
        material = True
    elif a["overlap"] is not None and a["overlap"] < 0.5:
        diffs.append({"field": "authors", "cited": ref.get("authors"), "record": rec.get("authors"),
                      "note": "only some cited authors appear in the record"})
    cy, ry = ref.get("year"), rec.get("year")
    if cy and ry and cy != ry:
        diffs.append({"field": "year", "cited": cy, "record": ry})
        if abs(cy - ry) > 1:
            material = True
    v = venue_check(ref.get("venue"), rec.get("venues") or [])
    if v is False:
        diffs.append({"field": "venue", "cited": ref.get("venue"), "record": rec.get("venues")})
    if ref.get("doi") and rec.get("doi") and ref["doi"].lower() != rec["doi"].lower():
        diffs.append({"field": "doi", "cited": ref["doi"], "record": rec["doi"]})
    if material:
        verdict = "mismatch"
    elif diffs:
        verdict = "verified_with_differences"
    else:
        verdict = "verified"
    # Nothing to compare except an identifier: say so rather than claiming a match.
    comparable = sim is not None or a["first_ok"] is not None or (cy and ry) or v is not None
    return {"verdict": verdict, "differences": diffs, "title_similarity": None if sim is None else round(sim, 2),
            "comparable_fields": bool(comparable)}


def candidate_accepted(ref: dict, rec: dict) -> bool:
    """Title-search candidates need a strong title match and agreeing year/author."""
    if not ref.get("title"):
        return False
    if title_similarity(ref["title"], rec.get("title")) < CANDIDATE_TITLE:
        return False
    cy, ry = ref.get("year"), rec.get("year")
    if cy and ry and abs(cy - ry) > 1:
        return False
    a = authors_check(ref.get("authors") or [], rec.get("authors") or [], bool(ref.get("et_al")))
    if a["first_ok"] is False:
        return False
    return True


def best_candidate(ref: dict, records: list[dict]) -> dict | None:
    ok = [r for r in records if candidate_accepted(ref, r)]
    if not ok:
        return None
    return max(ok, key=lambda r: title_similarity(ref["title"], r.get("title")))
