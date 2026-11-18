"""Contact selection (rubric 3.5): score all Hunter candidates, code enforces pass rules + role preference."""
from __future__ import annotations

from pydantic import BaseModel, Field

import llm
from research import scoring as sc
from scraper import discover as D
from scraper.clean import normalize
from scraper.fetch import OK, Fetcher

NO_SUITABLE_CONTACT = "NO_SUITABLE_CONTACT"


class CandJudgement(BaseModel):
    key: str
    scores: dict[str, int]
    reasons: dict[str, str] = Field(default_factory=dict)


class ContactJudgement(BaseModel):
    candidates: list[CandJudgement]


def first_party_texts(domain: str, research_log: dict, fetcher: Fetcher | None = None) -> tuple[dict[str, str], list[dict]]:
    """Ownership question: about/team/leadership pages (up to 2 new fetches) + pages already fetched."""
    texts = {u: p["text"] for u, p in research_log.get("_fetcher_pages", {}).items()}
    fetched_log = []
    cands = [u for u in research_log.get("discovery", {}).get("ownership_candidates", []) if u not in texts]
    if cands:
        f = fetcher or Fetcher(domain)
        try:
            for u in cands[:2]:
                p = f.fetch(u, "ownership question: leadership/team page for contact association")
                fetched_log.append({"url": u, "status": p["status"], "words": p.get("word_count", 0), "error": p.get("error")})
                if p["status"] == OK:
                    texts[u] = p["text"]
        finally:
            if fetcher is None:
                f.close()
    return texts, fetched_log


def name_on_pages(full_name: str, texts: dict[str, str]) -> str | None:
    n = normalize(full_name)
    if len(n.split()) < 2:
        return None
    for url, t in texts.items():
        if n in normalize(t):
            return url
    return None


def select(domain: str, company: str, hunter_data: dict, research_log: dict, fetcher: Fetcher | None = None) -> dict:
    emails = [e for e in hunter_data.get("emails", []) if e.get("first_name") or e.get("last_name")]
    result: dict = {"provider": hunter_data.get("_source"), "candidates": [], "selected": None, "status": None}
    if not emails:
        result.update(status=NO_SUITABLE_CONTACT, reason="Hunter returned no named candidates")
        return result
    texts, own_fetch = first_party_texts(domain, research_log, fetcher)
    result["ownership_pages"] = own_fetch
    cand_in = []
    for i, e in enumerate(emails):
        full = f"{e.get('first_name') or ''} {e.get('last_name') or ''}".strip()
        cand_in.append({"key": f"C{i + 1}", "name": full, "title": e.get("position"), "department": e.get("department"),
                        "seniority": e.get("seniority"), "hunter_confidence": e.get("confidence"),
                        "verification": (e.get("verification") or {}).get("status"),
                        "sources": [{"uri": s.get("uri"), "last_seen_on": s.get("last_seen_on")} for s in (e.get("sources") or [])[:3]],
                        "first_party_page": name_on_pages(full, texts)})
    q = research_log.get("qualification") or {}
    qual_txt = "\n".join(f"- {n}: level {d['level']} — {d['reason']}" for n, d in (q.get("dimensions") or {}).items())
    j, meta = llm.get_llm().json("contact", {"company": company, "domain": domain, "qualification": qual_txt,
                                             "candidates": cand_in}, ContactJudgement)
    result["llm"] = meta
    by_key = {c.key: c for c in j.candidates}
    scored: list[sc.ContactScore] = []
    for ci, e in zip(cand_in, emails):
        cj = by_key.get(ci["key"])
        scores = cj.scores if cj else {k: 0 for k in sc.S.CONTACT_CRITERIA}
        cs = sc.ContactScore(ci["name"], ci["title"] or "", e.get("value"), dict(scores),
                             reasons=cj.reasons if cj else {"note": "model did not score this candidate"},
                             on_first_party_page=bool(ci["first_party_page"]))
        sc.enforce_contact_rules(cs)
        scored.append(cs)
        result["candidates"].append({"name": cs.name, "title": cs.title, "email_research_only": cs.email,
                                     "role_category": cs.category, "scores": cs.scores, "total": cs.total,
                                     "passed": cs.passed, "reasons": cs.reasons, "first_party_page": ci["first_party_page"],
                                     "adjustments": cs.adjustments, "hunter_confidence": ci["hunter_confidence"]})
    best, why = sc.select_contact(scored, founder_led=bool(research_log.get("founder_led")))
    result["selection_reason"] = why
    if best is None:
        result["status"] = NO_SUITABLE_CONTACT
        return result
    result["status"] = "CONTACT_SELECTED"
    result["selected"] = {"name": best.name, "first_name": best.name.split()[0], "title": best.title,
                          "email_research_only": best.email, "total": best.total, "role_category": best.category}
    for c in result["candidates"]:
        c["selected"] = c["name"] == best.name
    return result
