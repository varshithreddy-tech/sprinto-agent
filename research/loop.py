"""Research loop (rubric 3.4): homepage + up to 3 pages, then max 3 rounds of up to 2 new pages.

Every round records: evidence gap -> selected source -> new evidence -> revised decision.
Code decides whether evidence is sufficient; the LLM only extracts facts and proposes levels.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from config import settings as S
from research import scoring as sc
from research.extract import Fact, extract, merge_facts
from research.qualify import apply_code_rules, judge
from scraper import discover as D
from scraper.fetch import OK, Fetcher


def _page_summary(p: dict) -> dict:
    return {"url": p["url"], "status": p["status"], "method": p.get("method"), "words": p.get("word_count", 0),
            "error": p.get("error"), "reason": p.get("reason"), "retrieval_date": p.get("retrieval_date"),
            "from_cache": bool(p.get("from_cache")), "attempts": p.get("attempts", 0)}


def _dims_view(result: sc.QualificationResult) -> dict:
    return {n: {"level": d.level, "llm_level": d.llm_level, "weight": d.weight, "points": d.points, "reason": d.reason,
                "zero_reason": d.zero_reason, "evidence_ids": d.evidence_ids, "adjustments": d.adjustments}
            for n, d in result.dims.items()}


def _questions_for_gaps(result: sc.QualificationResult) -> list[str]:
    gaps = sorted(result.gaps, key=lambda g: -S.DIMENSIONS[g])
    return list(dict.fromkeys(S.DIMENSION_QUESTION[g] for g in gaps))


def _select_pages(candidates: dict, questions: list[str], fetched: set[str]) -> list[tuple[str, str]]:
    picks: list[tuple[str, str]] = []
    # Round-robin over gap questions (highest weight first), max 2 new pages.
    pools = {q: D.pick_for_question(candidates, q, fetched, S.MAX_NEW_PAGES_PER_ROUND) for q in questions}
    while len(picks) < S.MAX_NEW_PAGES_PER_ROUND and any(pools.values()):
        for q in questions:
            if pools[q] and len(picks) < S.MAX_NEW_PAGES_PER_ROUND:
                u = pools[q].pop(0)
                if u not in [p[0] for p in picks]:
                    picks.append((u, q))
    return picks


def research_company(domain: str, fetcher: Fetcher | None = None, today: date | None = None,
                     progress=lambda msg: None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    f = fetcher or Fetcher(domain)
    log: dict = {"domain": domain, "rubric_version": S.RUBRIC_VERSION, "started_at": datetime.now(timezone.utc).isoformat(),
                 "rounds": [], "pages": [], "facts": [], "qualification": None, "status": None, "llm": []}
    facts: list[Fact] = []
    try:
        progress("fetching homepage")
        home = f.fetch(f"https://{domain}", "homepage")
        if home["status"] != OK:
            home = f.fetch(f"https://www.{domain}", "homepage (www)")
        if home["status"] != OK:
            log["pages"] = [_page_summary(p) for p in f.fetched.values()]
            if domain.lower().removeprefix("www.") in S.COMPETITOR_DOMAINS:
                # Rubric names direct compliance-platform competitors; config list applies even without page evidence.
                log["status"] = sc.DISQUALIFIED
                log["qualification"] = {"status": sc.DISQUALIFIED, "flags": {"competitor": True, "known_competitor_list_match": True,
                                        "web_evidence": f"none — homepage retrieval failed: {home.get('error')}"}}
                log["top_reason"] = (f"Compliance-platform competitor (configured competitor list; no web evidence because "
                                     f"homepage retrieval failed: {home.get('error')})")
                return log
            log["status"] = sc.RETRIEVAL_FAILED
            log["top_reason"] = f"Homepage retrieval failed: {home.get('error')} (retrieval failure, not poor fit)"
            return log
        disc = D.discover(f, home)
        log["discovery"] = {"total_urls": disc["total_urls"], "trust_subdomain": disc["trust_subdomain"],
                            "sitemaps": disc["sitemaps"], "candidate_counts": {q: len(v) for q, v in disc["candidates"].items()},
                            "ownership_candidates": disc["candidates"].get("ownership", [])[:6]}
        log["_candidates"] = disc["candidates"]

        # ---- initial round -------------------------------------------------
        selected = []
        for url, q in D.pick_initial(disc["candidates"], S.INITIAL_EXTRA_PAGES):
            progress(f"initial page [{q}] {url}")
            p = f.fetch(url, f"initial: {q}")
            if p["status"] != OK:
                # One substitute of the same question type: a blocked page is a retrieval failure, not poor fit.
                alt = D.pick_for_question(disc["candidates"], q, set(f.fetched), 1)
                if alt:
                    p = f.fetch(alt[0], f"initial: {q} (substitute for failed {url})")
            selected.append(p)
        pages = [home] + selected
        progress("extracting facts (round 0)")
        new, meta = extract(domain, pages, 0, today.isoformat())
        log["llm"].append({"step": "extract", "round": 0, **meta})
        facts = merge_facts(facts, new)
        j, meta = judge(domain, facts, today)
        log["llm"].append({"step": "qualify", "round": 0, **meta})

        def budget(rounds_done: int, res_gaps_q: list[str]) -> bool:
            return rounds_done < S.MAX_ADDITIONAL_ROUNDS and any(
                D.pick_for_question(disc["candidates"], q, set(f.fetched), 1) for q in res_gaps_q)

        # first pass assuming budget, then recompute with the real budget given the gaps
        result, flags = apply_code_rules(domain, j, facts, today, budget_remaining=True)
        result, flags = apply_code_rules(domain, j, facts, today, budget(0, _questions_for_gaps(result)))
        log["rounds"].append({"round": 0, "evidence_gap": "initial research: homepage + up to 3 relevant pages",
                              "selected_sources": [_page_summary(p) for p in pages],
                              "new_evidence": [x.id for x in facts if x.verified],
                              "unverified_evidence": [x.id for x in facts if not x.verified],
                              "decision": result.status, "total": result.total, "reasons": result.reasons})

        rounds = 0
        while result.status == sc.RESEARCH_MORE and rounds < S.MAX_ADDITIONAL_ROUNDS:
            rounds += 1
            questions = _questions_for_gaps(result)
            picks = _select_pages(disc["candidates"], questions, set(f.fetched))
            gap_txt = f"gaps {result.gaps} -> questions {questions}"
            if not picks:
                log["rounds"].append({"round": rounds, "evidence_gap": gap_txt, "selected_sources": [],
                                      "new_evidence": [], "decision": "no unfetched candidate page for these questions"})
                break
            new_pages = []
            for url, q in picks:
                progress(f"round {rounds} [{q}] {url}")
                new_pages.append(f.fetch(url, f"round {rounds}: {q} question"))
            before = {x.id for x in facts}
            new, meta = extract(domain, new_pages, rounds, today.isoformat())
            log["llm"].append({"step": "extract", "round": rounds, **meta})
            facts = merge_facts(facts, new)
            added = [x.id for x in facts if x.id not in before]
            j, meta = judge(domain, facts, today)
            log["llm"].append({"step": "qualify", "round": rounds, **meta})
            result, flags = apply_code_rules(domain, j, facts, today, budget_remaining=True)
            result, flags = apply_code_rules(domain, j, facts, today, budget(rounds, _questions_for_gaps(result)))
            log["rounds"].append({"round": rounds, "evidence_gap": gap_txt,
                                  "selected_sources": [_page_summary(p) for p in new_pages],
                                  "new_evidence": [x for x in added if next(ff for ff in facts if ff.id == x).verified],
                                  "decision": result.status, "total": result.total, "reasons": result.reasons})
        if result.status == sc.RESEARCH_MORE:
            result, flags = apply_code_rules(domain, j, facts, today, budget_remaining=False)

        log["status"] = result.status
        log["qualification"] = {"status": result.status, "total": result.total, "max_possible": result.max_possible,
                                "dimensions": _dims_view(result), "reasons": result.reasons, "gaps": result.gaps,
                                "flags": flags, "thresholds": {"min_total": S.QUALIFY_MIN_TOTAL,
                                                               "business_fit": S.QUALIFY_BUSINESS_FIT_LEVEL,
                                                               "min_compliance": S.QUALIFY_MIN_COMPLIANCE_LEVEL}}
        log["founder_led"] = flags.get("founder_led", False)
        log["additional_rounds"] = rounds
        log["top_reason"] = _top_reason(result, flags)
        return log
    finally:
        log["pages"] = [_page_summary(p) for p in f.fetched.values()]
        log["pages_fetched"] = len([p for p in f.fetched.values() if not p.get("from_cache")] ) or len(f.fetched)
        log["facts"] = [x.model_dump() for x in facts]
        log["_fetcher_pages"] = {u: p for u, p in f.fetched.items() if p.get("status") == OK}
        log.setdefault("additional_rounds", 0)
        f.close()


def _top_reason(result: sc.QualificationResult, flags: dict) -> str:
    if result.status == sc.DISQUALIFIED:
        if flags.get("competitor"):
            return "Compliance-platform competitor" + (" (known list + evidence)" if flags.get("known_competitor_list_match") else "") + f": {flags.get('competitor_reason', '')}"[:200]
        return f"Explicitly unrelated business: {flags.get('unrelated_reason', '')}"[:200]
    return "; ".join(result.reasons)[:240]
