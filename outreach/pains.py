"""Pain hypotheses (rubric 3.6). Gemini drafts + self-scores; code re-checks evidence, wording, capability, thresholds."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

import llm
from config import settings as S
from outreach import capabilities
from research import scoring as sc

INSUFFICIENT_PAIN_EVIDENCE = "INSUFFICIENT_PAIN_EVIDENCE"
TENTATIVE_RE = re.compile(r"\b(may|might|could|perhaps|possibly|likely|often|curious|wonder|if|whether|can)\b", re.I)
ASSERTIVE_BAD_RE = re.compile(r"\b(you are losing|you lose|you struggle|you're struggling|definitely|certainly|wasting)\b", re.I)


class Hypothesis(BaseModel):
    observed_fact: str
    evidence_ids: list[str] = Field(default_factory=list)
    hypothesis: str
    role_relevance: str = ""
    possible_consequence: str = ""
    capability: str
    scores: dict[str, int]


class PainsOut(BaseModel):
    hypotheses: list[Hypothesis]


def code_check(h: Hypothesis, valid_ids: set[str]) -> dict:
    scores = {k: min(max(int(h.scores.get(k, 0)), 0), m) for k, m in sc.PAIN_CRITERIA_MAX.items()}
    notes = []
    ev = [e for e in h.evidence_ids if e in valid_ids]
    if not ev:
        if scores["evidence"]:
            notes.append("evidence set to 0: no verified fact cited")
        scores["evidence"] = 0
    if not TENTATIVE_RE.search(h.hypothesis) or ASSERTIVE_BAD_RE.search(h.hypothesis):
        if scores["honest_wording"]:
            notes.append("honest_wording set to 0: hypothesis not tentative")
        scores["honest_wording"] = 0
    cap = h.capability.strip().lower()
    if cap not in S.APPROVED_CAPABILITIES:
        scores["capability_match"] = 0
        notes.append(f"capability_match set to 0: '{h.capability}' not approved")
    total = sc.pain_total(scores)
    ok, why = sc.pain_passes(scores, cap)
    return {**h.model_dump(), "evidence_ids": ev, "capability": cap, "scores": scores, "score": total,
            "passed": ok, "fail_reasons": why, "code_notes": notes}


def generate(company: str, contact: dict, facts: list[dict], qualification: dict) -> dict:
    verified = [f for f in facts if f.get("verified")]
    valid = {f["id"] for f in verified}
    facts_in = [{"id": f["id"], "field": f["field"], "value": f["value"], "excerpt": f["exact_excerpt"],
                 "source_url": f["source_url"], "publication_date": f.get("publication_date")} for f in verified]
    qual_txt = "\n".join(f"- {n}: level {d['level']} — {d['reason']}" for n, d in (qualification.get("dimensions") or {}).items())
    out, meta = llm.get_llm().json("pains", {"name": contact["name"], "title": contact["title"], "company": company,
                                             "capabilities": capabilities.as_prompt_text(), "facts": facts_in,
                                             "qualification": qual_txt}, PainsOut)
    checked = [code_check(h, valid) for h in out.hypotheses[:2]]
    ok, why = sc.pains_pass(checked)
    return {"status": "PAINS_OK" if ok else INSUFFICIENT_PAIN_EVIDENCE, "hypotheses": checked, "fail_reasons": why,
            "llm": meta}
