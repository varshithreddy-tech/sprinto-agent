"""Email QA (rubric 3.7): Gemini reviews; code applies deterministic checks + thresholds; max 2 revisions."""
from __future__ import annotations

from pydantic import BaseModel, Field

import llm
from config import settings as S
from outreach.email_draft import draft, facts_text, supporting_ids
from research import scoring as sc

EMAIL_QA_FAILED = "EMAIL_QA_FAILED"


class QAOut(BaseModel):
    scores: dict[str, int]
    checks: dict[str, bool]
    issues: list[str] = Field(default_factory=list)
    revision_instructions: str = ""


def review(company: str, contact: dict, pain: dict, facts: list[dict], subject: str, body: str) -> tuple[sc.EmailQA, QAOut, dict]:
    out, meta = llm.get_llm().json("email_qa", {
        "name": contact["name"], "title": contact["title"], "company": company, "subject": subject, "body": body,
        "facts": facts_text(facts, supporting_ids(pain, facts)), "pain": pain["hypothesis"], "capability": pain["capability"],
    }, QAOut)
    qa = sc.combine_email_qa(out.scores, out.checks, subject, body, contact["first_name"])
    return qa, out, meta


def write_and_qa(company: str, contact: dict, pain: dict, facts: list[dict]) -> dict:
    """First draft + up to EMAIL_MAX_REVISIONS revisions. Returns all versions with QA."""
    versions = []
    notes = ""
    for v in range(1, S.EMAIL_MAX_REVISIONS + 2):
        d, dmeta = draft(company, contact, pain, facts, notes)
        qa, out, qmeta = review(company, contact, pain, facts, d.subject, d.body)
        versions.append({"version": v, "subject": d.subject, "body": d.body, "qa_scores": qa.scores, "qa_checks": qa.checks,
                         "qa_total": qa.total, "words": qa.words, "passed": qa.passed, "issues": out.issues,
                         "code_notes": qa.notes, "draft_prompt": dmeta.get("prompt_version"),
                         "qa_prompt": qmeta.get("prompt_version")})
        if qa.passed:
            return {"status": "EMAIL_QA_PASSED", "final_version": v, "versions": versions}
        failed_checks = [k for k, ok in qa.checks.items() if not ok]
        notes = "\n".join([f"- total {qa.total}/10 (need {S.EMAIL_PASS_MIN})",
                           *(f"- failed check: {k}" for k in failed_checks), *(f"- {i}" for i in out.issues),
                           *(f"- {n}" for n in qa.notes), out.revision_instructions])
    return {"status": EMAIL_QA_FAILED, "final_version": None, "versions": versions}
