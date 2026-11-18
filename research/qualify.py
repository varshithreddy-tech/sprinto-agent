"""Qualification: Gemini proposes per-dimension levels; code validates evidence, checks date windows, scores, routes."""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

import llm
from config import settings as S
from research import scoring as sc
from research.extract import Fact, parse_date


class DimJudgement(BaseModel):
    level: int = 0
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    zero_reason: Optional[str] = None


class Flag(BaseModel):
    value: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class TimingSignal(BaseModel):
    fact_id: str
    signal_type: str


class QualJudgement(BaseModel):
    dimensions: dict[str, DimJudgement]
    timing_signals: list[TimingSignal] = Field(default_factory=list)
    competitor: Flag = Flag()
    explicitly_unrelated: Flag = Flag()
    founder_led: Flag = Flag()


def facts_for_prompt(facts: list[Fact]) -> list[dict]:
    return [{"id": f.id, "field": f.field, "value": f.value, "label": f.label, "source_url": f.source_url,
             "excerpt": f.exact_excerpt, "publication_date": f.publication_date, "signal_type": f.signal_type}
            for f in facts if f.verified]


def _known_competitor(domain: str, facts: list[Fact]) -> bool:
    d = domain.lower().removeprefix("www.")
    return d in S.COMPETITOR_DOMAINS


def judge(domain: str, facts: list[Fact], today: date) -> tuple[QualJudgement, dict]:
    return llm.get_llm().json("qualify", {"domain": domain, "today": today.isoformat(),
                                          "facts": facts_for_prompt(facts)}, QualJudgement)


def apply_code_rules(domain: str, j: QualJudgement, facts: list[Fact], today: date,
                     budget_remaining: bool) -> tuple[sc.QualificationResult, dict]:
    valid = {f.id: f for f in facts if f.verified}
    dims: dict[str, sc.DimensionScore] = {}
    for name in S.DIMENSIONS:
        dj = j.dimensions.get(name, DimJudgement(level=0, reason="not judged", zero_reason="missing"))
        level = dj.level if dj.level in (0, 1, 2) else 0
        ev = [e for e in dj.evidence_ids if e in valid]
        ds = sc.DimensionScore(name, level, dj.reason, dj.zero_reason, ev, llm_level=dj.level)
        if level > 0 and not ev:
            ds.level, ds.zero_reason = 0, "missing"
            ds.adjustments.append("level set to 0: no verified evidence cited")
        if ds.level == 0 and ds.zero_reason not in ("missing", "negative"):
            ds.zero_reason = "missing"
        if ds.level > 0:
            ds.zero_reason = None
        dims[name] = ds

    # Timing: the code decides from dated, verified signals.
    signals = []
    for s in j.timing_signals:
        f = valid.get(s.fact_id)
        if f:
            signals.append({"signal_type": s.signal_type, "date": parse_date(f.publication_date), "fact_id": f.id})
    t_level, t_why = sc.timing_level(signals, today)
    rn = dims["reason_now"]
    if t_level != rn.level:
        rn.adjustments.append(f"code window check: level {rn.level} -> {t_level} ({t_why})")
    rn.level = t_level
    if t_level == 0:
        rn.zero_reason = "missing"  # undated/out-of-window: more recent dated evidence could exist
        rn.evidence_ids = [s["fact_id"] for s in signals]
    else:
        rn.zero_reason = None
        rn.evidence_ids = sorted({s["fact_id"] for s in signals if s["date"]})

    competitor = _known_competitor(domain, facts) or (j.competitor.value and any(e in valid for e in j.competitor.evidence_ids))
    unrelated = j.explicitly_unrelated.value and any(e in valid for e in j.explicitly_unrelated.evidence_ids)
    if unrelated:
        bf = dims["business_fit"]
        bf.level, bf.zero_reason = 0, "negative"
    result = sc.qualify(dims, is_competitor=competitor, explicitly_unrelated=unrelated,
                        budget_remaining=budget_remaining)
    flags = {"competitor": competitor, "competitor_reason": j.competitor.reason,
             "known_competitor_list_match": _known_competitor(domain, facts),
             "explicitly_unrelated": unrelated, "unrelated_reason": j.explicitly_unrelated.reason,
             "founder_led": j.founder_led.value and any(e in valid for e in j.founder_led.evidence_ids),
             "timing_check": t_why}
    return result, flags
