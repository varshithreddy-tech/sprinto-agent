from datetime import date, timedelta

import pytest

from research import scoring as sc

TODAY = date(2026, 11, 18)


def dims(**levels):
    base = dict(business_fit=2, trust_exposure=2, compliance_workload=2, reason_now=0, delivery_complexity=0)
    base.update(levels)
    out = {}
    for k, v in base.items():
        if isinstance(v, tuple):
            out[k] = sc.DimensionScore(k, v[0], zero_reason=v[1])
        else:
            out[k] = sc.DimensionScore(k, v, zero_reason="missing" if v == 0 else None)
    return out


def test_points_formula():
    assert sc.points(25, 2) == 25
    assert sc.points(25, 1) == 12.5
    assert sc.points(10, 0) == 0
    with pytest.raises(ValueError):
        sc.points(25, 3)


def test_qualified_at_70():
    r = sc.qualify(dims(), is_competitor=False, explicitly_unrelated=False, budget_remaining=True)
    assert r.total == 70 and r.status == sc.QUALIFIED


def test_business_fit_must_be_2():
    d = dims(business_fit=1, reason_now=2, delivery_complexity=2)  # 12.5+20+25+20+10 = 87.5
    r = sc.qualify(d, is_competitor=False, explicitly_unrelated=False, budget_remaining=False)
    assert r.total == 87.5 and r.status == sc.LOW_PRIORITY


def test_compliance_workload_required():
    d = dims(compliance_workload=(0, "negative"), reason_now=2, delivery_complexity=2)  # 25+20+0+20+10 = 75
    r = sc.qualify(d, is_competitor=False, explicitly_unrelated=False, budget_remaining=True)
    assert r.status == sc.LOW_PRIORITY


def test_missing_compliance_evidence_triggers_research_then_insufficient():
    d = dims(compliance_workload=0, reason_now=2)  # missing
    r = sc.qualify(d, is_competitor=False, explicitly_unrelated=False, budget_remaining=True)
    assert r.status == sc.RESEARCH_MORE and "compliance_workload" in r.gaps
    r = sc.qualify(d, is_competitor=False, explicitly_unrelated=False, budget_remaining=False)
    assert r.status == sc.INSUFFICIENT_EVIDENCE


def test_below_threshold_even_best_case_is_low_priority():
    d = dims(business_fit=2, trust_exposure=(0, "negative"), compliance_workload=1,
             reason_now=(0, "negative"), delivery_complexity=0)
    # 25 + 0 + 12.5 + 0 + 0 = 37.5 ; best case +10 = 47.5 < 65
    r = sc.qualify(d, is_competitor=False, explicitly_unrelated=False, budget_remaining=True)
    assert r.status == sc.LOW_PRIORITY


def test_competitor_and_unrelated_disqualify():
    assert sc.qualify(dims(), is_competitor=True, explicitly_unrelated=False, budget_remaining=True).status == sc.DISQUALIFIED
    assert sc.qualify(dims(), is_competitor=False, explicitly_unrelated=True, budget_remaining=True).status == sc.DISQUALIFIED


def test_timing_windows():
    d = lambda days: TODAY - timedelta(days=days)
    assert sc.timing_level([{"signal_type": "relevant_hiring", "date": d(30)}], TODAY)[0] == 2
    assert sc.timing_level([{"signal_type": "relevant_hiring", "date": d(200)}], TODAY)[0] == 1
    assert sc.timing_level([{"signal_type": "expansion", "date": d(30)}], TODAY)[0] == 1
    assert sc.timing_level([{"signal_type": "expansion", "date": d(400)}], TODAY)[0] == 0
    assert sc.timing_level([{"signal_type": "audit_initiative", "date": None}], TODAY)[0] == 0
    assert sc.timing_level([{"signal_type": "compliance_deadline", "date": TODAY + timedelta(days=60)}], TODAY)[0] == 2


def cand(name, title, assoc=2, func=2, drive=2, match=1, ident=2, first_party=True):
    c = sc.ContactScore(name, title, None, dict(company_association=assoc, functional_ownership=func,
                                                ability_to_drive=drive, match_to_need=match, identity_quality=ident),
                        on_first_party_page=first_party)
    return sc.enforce_contact_rules(c)


def test_role_categories():
    assert sc.role_category("Head of GRC") == 1
    assert sc.role_category("CISO") == 2
    assert sc.role_category("VP Engineering") == 3
    assert sc.role_category("Co-Founder & CEO") == 4
    assert sc.role_category("Marketing Manager") == 9


def test_contact_pass_rules():
    assert cand("A", "CISO").passed
    assert not cand("A", "CISO", assoc=0, match=2).passed
    assert not cand("A", "CISO", func=0, match=2, ident=2).passed
    assert not cand("A", "CISO", drive=1, match=0).passed  # 7/10


def test_association_capped_without_first_party_page():
    c = cand("A", "CISO", first_party=False)
    assert c.scores["company_association"] == 1 and c.total == 8


def test_role_preference_not_first_result():
    cands = [cand("Cto Person", "CTO", match=2), cand("Sec Person", "Head of Security"),
             cand("Grc Person", "GRC Lead", drive=1, match=2)]
    best, _ = sc.select_contact(cands, founder_led=False)
    assert best.name == "Grc Person"


def test_founder_only_when_founder_led():
    cands = [cand("Founder", "Co-founder & CEO", match=2)]
    assert sc.select_contact(cands, founder_led=False)[0] is None
    assert sc.select_contact(cands, founder_led=True)[0].name == "Founder"


def test_no_suitable_contact():
    best, why = sc.select_contact([cand("M", "Marketing Manager", func=0)], founder_led=True)
    assert best is None and why.startswith("NO_SUITABLE_CONTACT")


def pain(e=3, r=2, c=2, m=2, h=1, cap="evidence collection", fact="f1"):
    return {"scores": dict(evidence=e, role_relevance=r, consequence=c, capability_match=m, honest_wording=h),
            "capability": cap, "observed_fact": fact}


def test_pain_rules():
    assert sc.pain_total(pain()["scores"]) == 10
    assert sc.pains_pass([pain(), pain(cap="compliance preparation", fact="f2")])[0]
    assert not sc.pains_pass([pain()])[0]
    assert not sc.pains_pass([pain(), pain(e=1, fact="f2")])[0]          # evidence < 2
    assert not sc.pains_pass([pain(), pain(h=0, fact="f2")])[0]          # dishonest wording
    assert not sc.pains_pass([pain(), pain(cap="pricing", fact="f2")])[0]
    assert not sc.pains_pass([pain(), pain()])[0]                        # not distinct


def test_pain_threshold_exact():
    ok, _ = sc.pain_passes(pain(r=0, c=1)["scores"], "evidence collection")  # 3+0+1+2+1 = 7
    assert ok
    ok, _ = sc.pain_passes(pain(r=0, c=0)["scores"], "evidence collection")  # 6
    assert not ok


GOOD_BODY = ("Hi Priya,\n\n" + "word " * 90).strip()


def test_email_qa_combination():
    scores = dict(specific_hook=2, useful_hypothesis=2, relevant_value=2, simple_next_step=1, clarity=2)
    checks = {k: True for k in sc.MANDATORY_CHECKS}
    qa = sc.combine_email_qa(scores, checks, "SOC 2 evidence at Acme", GOOD_BODY, "Priya")
    assert qa.total == 9 and qa.passed
    qa = sc.combine_email_qa(scores, checks, "Re: SOC 2", GOOD_BODY, "Priya")
    assert not qa.passed
    qa = sc.combine_email_qa(scores, checks, "SOC 2", GOOD_BODY.replace("Priya", "there"), "Priya")
    assert not qa.passed
    short = "Hi Priya, quick question."
    qa = sc.combine_email_qa(scores, checks, "SOC 2", short, "Priya")
    assert qa.scores["clarity"] == 1 and qa.total == 8
    qa = sc.combine_email_qa(scores, checks, "SOC 2", GOOD_BODY + " we guarantee 50% savings", "Priya")
    assert not qa.passed
