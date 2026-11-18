"""Pure scoring code. No I/O, no LLM. The LLM proposes levels; this module decides.

Covers: qualification (3.3), contact selection (3.5), pain hypotheses (3.6), email QA (3.7).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from config import settings as S

# --------------------------------------------------------------------------
# 3.3 Company qualification
# --------------------------------------------------------------------------

QUALIFIED = "QUALIFIED"
LOW_PRIORITY = "LOW_PRIORITY"
DISQUALIFIED = "DISQUALIFIED"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
RESEARCH_MORE = "RESEARCH_MORE"
RETRIEVAL_FAILED = "RETRIEVAL_FAILED"

TIMING_L2_SIGNALS = {"audit_initiative", "relevant_hiring", "compliance_deadline"}
TIMING_L1_SIGNALS = {"expansion", "enterprise_launch"} | TIMING_L2_SIGNALS


@dataclass
class DimensionScore:
    name: str
    level: int                       # 0, 1, 2 (after code adjustments)
    reason: str = ""
    zero_reason: str | None = None   # "missing" | "negative" when level == 0
    evidence_ids: list[str] = field(default_factory=list)
    llm_level: int | None = None     # what the model proposed, before code checks
    adjustments: list[str] = field(default_factory=list)

    @property
    def weight(self) -> int:
        return S.DIMENSIONS[self.name]

    @property
    def points(self) -> float:
        return points(self.weight, self.level)


def points(weight: int, level: int) -> float:
    if level not in (0, 1, 2):
        raise ValueError(f"level must be 0/1/2, got {level}")
    return weight * level / 2


def total_points(dims: Iterable[DimensionScore]) -> float:
    return sum(d.points for d in dims)


def timing_level(signals: list[dict], today: date) -> tuple[int, str]:
    """Code-side window check for 'Reason to engage now'.

    signals: [{"signal_type": str, "date": date|None}]
    Level 2: audit initiative / relevant hiring / compliance deadline dated within 6 months.
    Level 1: any of those or dated expansion/enterprise launch within 12 months.
    Undated or out-of-window signals score 0.
    """
    best, why = 0, "no dated timing signal inside the 12-month window"
    for s in signals:
        d, kind = s.get("date"), s.get("signal_type")
        if d is None:
            continue
        age = (today - d).days
        if age < -730:  # implausible far-future date: ignore
            continue
        age = max(age, 0)  # future deadlines count as current
        if kind in TIMING_L2_SIGNALS and age <= S.TIMING_L2_WINDOW_DAYS:
            return 2, f"{kind} dated {d.isoformat()} ({age} days old) within 6 months"
        if kind in TIMING_L1_SIGNALS and age <= S.TIMING_L1_WINDOW_DAYS and best < 1:
            best, why = 1, f"{kind} dated {d.isoformat()} ({age} days old) within 12 months"
    return best, why


@dataclass
class QualificationResult:
    status: str
    total: float
    dims: dict[str, DimensionScore]
    reasons: list[str]
    gaps: list[str]                 # dimensions at 0 because evidence is missing
    max_possible: float


def qualify(dims: dict[str, DimensionScore], *, is_competitor: bool, explicitly_unrelated: bool,
            budget_remaining: bool) -> QualificationResult:
    """Apply thresholds + routing. Deterministic."""
    missing = set(S.DIMENSIONS) - set(dims)
    if missing:
        raise ValueError(f"missing dimensions: {missing}")
    total = total_points(dims.values())
    gaps = [n for n, d in dims.items() if d.level == 0 and d.zero_reason != "negative"]
    # Best case if every evidence gap scored level 2.
    max_possible = total + sum(points(dims[g].weight, 2) for g in gaps)
    bf = dims["business_fit"].level
    cw = dims["compliance_workload"].level
    reasons: list[str] = []

    if is_competitor:
        return QualificationResult(DISQUALIFIED, total, dims,
                                   ["Direct compliance-automation platform competitor"], gaps, max_possible)
    if explicitly_unrelated:
        return QualificationResult(DISQUALIFIED, total, dims,
                                   ["Explicit evidence the business is unrelated (not software/SaaS handling customer data)"],
                                   gaps, max_possible)

    passes = total >= S.QUALIFY_MIN_TOTAL and bf == S.QUALIFY_BUSINESS_FIT_LEVEL and cw >= S.QUALIFY_MIN_COMPLIANCE_LEVEL
    if passes:
        reasons.append(f"Total {total:g} >= {S.QUALIFY_MIN_TOTAL}, business fit = 2, compliance workload = {cw}")
        return QualificationResult(QUALIFIED, total, dims, reasons, gaps, max_possible)

    if total < S.QUALIFY_MIN_TOTAL:
        reasons.append(f"Total {total:g} < {S.QUALIFY_MIN_TOTAL}")
    if bf != 2:
        reasons.append(f"Business fit level {bf} (needs 2)")
    if cw < 1:
        reasons.append(f"Compliance workload level {cw} (needs >= 1)")

    # Could filling the gaps flip the decision?
    could_pass = (max_possible >= S.QUALIFY_MIN_TOTAL
                  and (bf == 2 or "business_fit" in gaps)
                  and (cw >= 1 or "compliance_workload" in gaps))
    if could_pass and gaps:
        status = RESEARCH_MORE if budget_remaining else INSUFFICIENT_EVIDENCE
        reasons.append(f"Missing evidence on {', '.join(gaps)}; best case {max_possible:g}")
        return QualificationResult(status, total, dims, reasons, gaps, max_possible)
    return QualificationResult(LOW_PRIORITY, total, dims, reasons, gaps, max_possible)


# --------------------------------------------------------------------------
# 3.5 Contact selection
# --------------------------------------------------------------------------

ROLE_PATTERNS = [
    (1, r"\b(grc|compliance|governance|risk|audit|trust)\b"),
    (2, r"\b(ciso|chief information security|security|infosec|cybersecurity)\b"),
    (3, r"\b(cto|chief technology|vp,? engineering|vp of engineering|vice president,? engineering|head of engineering|engineering director|director of engineering|vp,? technology)\b"),
    (4, r"\b(founder|co-founder|cofounder|ceo|chief executive)\b"),
]
SENIOR_PATTERN = r"\b(chief|vp|vice president|head|director|lead|manager|founder|ceo|cto|ciso|officer)\b"


def role_category(title: str | None) -> int:
    """1 Compliance/GRC, 2 Security, 3 CTO/VP Eng, 4 Founder/CEO, 9 other. Lowest wins."""
    t = (title or "").lower()
    for cat, pat in ROLE_PATTERNS:
        if re.search(pat, t):
            # "Customer success manager, trust & safety" etc. still counts as adjacent; seniority handled by LLM.
            return cat
    return 9


@dataclass
class ContactScore:
    name: str
    title: str
    email: str | None
    scores: dict[str, int]
    reasons: dict[str, str] = field(default_factory=dict)
    on_first_party_page: bool = False
    adjustments: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.scores.get(c, 0) for c in S.CONTACT_CRITERIA)

    @property
    def category(self) -> int:
        return role_category(self.title)

    @property
    def passed(self) -> bool:
        return (self.total >= S.CONTACT_PASS_MIN
                and self.scores.get("company_association", 0) > 0
                and self.scores.get("functional_ownership", 0) > 0)


def enforce_contact_rules(c: ContactScore) -> ContactScore:
    """Code guards on LLM-proposed contact scores."""
    for k in S.CONTACT_CRITERIA:
        v = int(c.scores.get(k, 0))
        c.scores[k] = min(max(v, 0), 2)
    if c.scores["company_association"] == 2 and not c.on_first_party_page:
        c.scores["company_association"] = 1
        c.adjustments.append("company_association capped at 1: name not found on company's own about/team page")
    if not c.name or not c.title:
        if c.scores["identity_quality"] > 0:
            c.adjustments.append("identity_quality set to 0: missing name or title")
        c.scores["identity_quality"] = 0
    return c


def select_contact(cands: list[ContactScore], *, founder_led: bool) -> tuple[ContactScore | None, str]:
    """Evaluate all candidates; apply pass rules then role preference. Never 'first result'."""
    passing = [c for c in cands if c.passed]
    if not passing:
        return None, "NO_SUITABLE_CONTACT: no candidate reached 8/10 with association and functional ownership > 0"
    security_owner = [c for c in passing if c.category in (1, 2)]
    if security_owner:
        pool = security_owner
    else:
        pool = [c for c in passing if c.category == 3]
        if not pool and founder_led:
            pool = [c for c in passing if c.category == 4]
        if not pool:
            pool = [c for c in passing if c.category not in (3, 4, 9)] or []
    if not pool:
        return None, ("NO_SUITABLE_CONTACT: passing candidates only in non-preferred roles"
                      + ("" if founder_led else " (founder/CEO not allowed: founder-led context not supported)"))
    best = sorted(pool, key=lambda c: (c.category, -c.total, c.name))[0]
    return best, f"Selected role category {best.category} with {best.total}/10 among {len(cands)} candidates"


# --------------------------------------------------------------------------
# 3.6 Pain hypotheses
# --------------------------------------------------------------------------

PAIN_CRITERIA_MAX = {"evidence": 3, "role_relevance": 2, "consequence": 2, "capability_match": 2, "honest_wording": 1}


def pain_total(scores: dict[str, int]) -> int:
    return sum(min(max(int(scores.get(k, 0)), 0), m) for k, m in PAIN_CRITERIA_MAX.items())


def pain_passes(scores: dict[str, int], capability: str) -> tuple[bool, list[str]]:
    why = []
    t = pain_total(scores)
    if t < S.PAIN_MIN_SCORE:
        why.append(f"score {t} < {S.PAIN_MIN_SCORE}")
    if int(scores.get("evidence", 0)) < S.PAIN_MIN_EVIDENCE:
        why.append(f"evidence {scores.get('evidence', 0)} < {S.PAIN_MIN_EVIDENCE}")
    if int(scores.get("honest_wording", 0)) != 1:
        why.append("honest wording != 1")
    if capability.strip().lower() not in S.APPROVED_CAPABILITIES:
        why.append(f"capability '{capability}' not in approved list")
    return (not why), why


def pains_pass(pains: list[dict]) -> tuple[bool, list[str]]:
    """pains: [{"scores":{...}, "capability": str, "observed_fact": str}] — need two distinct passing."""
    why = []
    if len(pains) < 2:
        return False, ["fewer than two hypotheses"]
    for i, p in enumerate(pains[:2]):
        ok, w = pain_passes(p["scores"], p["capability"])
        if not ok:
            why.append(f"hypothesis {i + 1}: " + "; ".join(w))
    a, b = pains[0], pains[1]
    if (a["capability"].lower() == b["capability"].lower()
            and _norm(a.get("observed_fact", "")) == _norm(b.get("observed_fact", ""))):
        why.append("hypotheses are not distinct (same fact and capability)")
    return (not why), why


# --------------------------------------------------------------------------
# 3.7 Email QA
# --------------------------------------------------------------------------

MANDATORY_CHECKS = [
    "correct_person_and_company", "evidence_for_every_claim", "tentative_hypotheses",
    "no_invented_metrics_or_guarantees", "no_false_re_or_follow_up", "no_unsupported_deficiency",
    "addresses_by_name", "one_primary_pain", "no_generic_praise_jargon_familiarity",
]


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def code_email_checks(subject: str, body: str, first_name: str) -> dict[str, bool]:
    """Deterministic checks the code can do itself (overrides LLM if it says pass)."""
    low_s, low_b = subject.lower().strip(), body.lower()
    first_line = next((ln for ln in body.strip().splitlines() if ln.strip()), "")
    return {
        "no_false_re_or_follow_up": not (low_s.startswith(("re:", "fw:", "fwd:"))
                                        or "following up" in low_b or "follow up on my" in low_b
                                        or "as promised" in low_b or "circling back" in low_b),
        "addresses_by_name": bool(first_name) and first_name.lower() in first_line.lower(),
        "no_invented_metrics_or_guarantees": not re.search(r"\b(guarantee[ds]?|\d+\s?%|\d+x faster|in \d+ (days|weeks))\b", low_b),
    }


@dataclass
class EmailQA:
    scores: dict[str, int]
    checks: dict[str, bool]
    words: int
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(min(max(int(self.scores.get(k, 0)), 0), 2) for k in S.EMAIL_CRITERIA)

    @property
    def passed(self) -> bool:
        return self.total >= S.EMAIL_PASS_MIN and all(self.checks.get(k, False) for k in MANDATORY_CHECKS)


def combine_email_qa(llm_scores: dict[str, int], llm_checks: dict[str, bool], subject: str, body: str,
                     first_name: str) -> EmailQA:
    scores = {k: min(max(int(llm_scores.get(k, 0)), 0), 2) for k in S.EMAIL_CRITERIA}
    checks = {k: bool(llm_checks.get(k, False)) for k in MANDATORY_CHECKS}
    notes = []
    for k, v in code_email_checks(subject, body, first_name).items():
        if not v and checks.get(k):
            notes.append(f"code check failed: {k}")
        checks[k] = checks[k] and v
    wc = word_count(body)
    lo, hi = S.EMAIL_WORDS_TOLERANCE
    if not (lo <= wc <= hi) and scores["clarity"] > 1:
        scores["clarity"] = 1
        notes.append(f"clarity capped at 1: {wc} words outside {lo}-{hi}")
    return EmailQA(scores, checks, wc, notes)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()
