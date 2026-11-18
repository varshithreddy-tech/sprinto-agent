"""Email drafting (separate Gemini call from QA)."""
from __future__ import annotations

from pydantic import BaseModel

import llm
from outreach import capabilities


class Draft(BaseModel):
    subject: str
    body: str


def facts_text(facts: list[dict], ids: list[str] | None = None) -> str:
    use = [f for f in facts if f.get("verified") and (ids is None or f["id"] in ids)]
    return "\n".join(f"- [{f['id']}] {f['field']}: {f['value']} (\"{f['exact_excerpt'][:160]}\" — {f['source_url']})" for f in use)


def supporting_ids(pain: dict, facts: list[dict]) -> list[str]:
    ids = list(pain.get("evidence_ids", []))
    extra = [f["id"] for f in facts if f.get("verified") and f["field"] in ("company_name", "product", "compliance_claims")]
    return list(dict.fromkeys(ids + extra))[:14]


def draft(company: str, contact: dict, pain: dict, facts: list[dict], revision_notes: str = "") -> tuple[Draft, dict]:
    caps = capabilities.load()
    ids = supporting_ids(pain, facts)
    hook = facts_text(facts, pain.get("evidence_ids", [])[:2]) or pain["observed_fact"]
    d, meta = llm.get_llm().json("email_draft", {
        "name": contact["name"], "first_name": contact["first_name"], "title": contact["title"], "company": company,
        "hook_fact": hook, "pain": pain["hypothesis"], "capability": pain["capability"],
        "capability_desc": caps.get(pain["capability"], {}).get("description", ""),
        "facts": facts_text(facts, ids),
        "revision_notes": ("\nREVISION — fix these QA issues from the previous draft:\n" + revision_notes) if revision_notes else "",
    }, Draft)
    return d, meta
