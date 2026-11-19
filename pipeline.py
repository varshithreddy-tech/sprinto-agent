"""End-to-end orchestration for one domain, and the batch runner.

research -> qualification -> Hunter contact -> two pains -> email + QA -> send -> open reply thread.
One account's failure never stops the batch.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta, timezone

from config import settings as S
from llm import LLMAuthError
from outreach import contact as contact_mod
from outreach import email_qa, hunter, pains as pains_mod, send as send_mod
from research import scoring as sc
from research.extract import Fact, extract, merge_facts
from research.loop import research_company
from scraper import discover as D
from scraper.fetch import Fetcher
from storage import db
from outreach.records import (DRAFT_READY, make_draft, company_name, finalise_reconciled_send, open_thread as _open_thread,  # noqa: F401
                               public_log as _public_log, reconcile_sends, save_evidence, save_log, sender_for_name)

TERMINAL_SENT = {"EMAIL_SENT", "DRY_RUN_SENT"}
DONE_STATUSES = TERMINAL_SENT | {"DRAFT_READY"}  # re-runs skip these unless --force


def _extra_research_for_pains(domain: str, log: dict) -> dict | None:
    """Pain evidence missing: research within the remaining round budget (one round, up to 2 pages)."""
    if log.get("additional_rounds", 0) >= S.MAX_ADDITIONAL_ROUNDS:
        return None
    cands = log.get("_candidates", {})
    fetched = {p["url"] for p in log.get("pages", [])}
    picks = []
    for q in ("compliance", "timing", "buyer"):
        picks += [(u, q) for u in D.pick_for_question(cands, q, fetched | {p[0] for p in picks}, 1)]
    picks = picks[:S.MAX_NEW_PAGES_PER_ROUND]
    if not picks:
        return None
    f = Fetcher(domain)
    try:
        pages = [f.fetch(u, f"pain-evidence round: {q}") for u, q in picks]
    finally:
        f.close()
    rnd = log.get("additional_rounds", 0) + 1
    today = datetime.now(timezone.utc).date().isoformat()
    new, meta = extract(domain, pages, rnd, today)
    facts = merge_facts([Fact(**x) for x in log["facts"]], new)
    log["facts"] = [x.model_dump() for x in facts]
    log["additional_rounds"] = rnd
    log["rounds"].append({"round": rnd, "evidence_gap": "pain hypotheses lacked account-specific evidence",
                          "selected_sources": [{"url": p["url"], "status": p["status"], "words": p.get("word_count")} for p in pages],
                          "new_evidence": [x.id for x in facts if x.round == rnd and x.verified],
                          "decision": "re-generate pain hypotheses"})
    for p in pages:
        if p["status"] == "OK":
            log.setdefault("_fetcher_pages", {})[p["url"]] = p
    log["pages"] += [{"url": p["url"], "status": p["status"], "words": p.get("word_count"), "reason": p.get("reason")} for p in pages]
    log["pages_fetched"] = log.get("pages_fetched", 0) + len(pages)
    return log


def process_domain(domain: str, *, mock_hunter: bool = False, dry_run: bool = False, integrations: dict | None = None,
                   progress=lambda d, m: None, force: bool = False, run_id: str = "") -> dict:
    integrations = integrations if integrations is not None else {}
    domain = domain.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
    existing = db.query_one("SELECT status FROM companies WHERE domain=?", (domain,))
    # Resume: skip domains already drafted or sent (re-running would spend credits and replace the draft).
    if existing and existing["status"] in DONE_STATUSES and not force:
        progress(domain, f"already {existing['status']} — skipped, nothing re-run or sent")
        row = db.query_one("SELECT * FROM companies WHERE domain=?", (domain,))
        return {**row, "skipped": True, "existing_status": existing["status"]}
    p = lambda m: progress(domain, m)
    log = research_company(domain, progress=p)
    log["run_id"] = run_id
    log["mode"] = {"mock_hunter": mock_hunter, "dry_run": dry_run}
    save_evidence(domain, log["facts"])
    save_log(domain, log)
    db.log_activity_safe(domain, "QUALIFICATION", {"status": log["status"], "total": (log.get("qualification") or {}).get("total")})
    if log["status"] != sc.QUALIFIED:
        return log

    company = company_name(log)
    # ---- Hunter ------------------------------------------------------------
    if integrations.get("hunter_blocked") and not mock_hunter:
        log["status"] = "HUNTER_PAUSED"
        log["escalation_reason"] = integrations["hunter_blocked"]
        save_log(domain, log)
        return log
    try:
        p("Hunter domain search" + (" (mock)" if mock_hunter else ""))
        hdata = hunter.domain_search(domain, mock=mock_hunter)
    except hunter.HunterQuotaError as e:
        integrations["hunter_blocked"] = f"Hunter quota exhausted: {e}"
        log.update(status="HUNTER_QUOTA_PAUSED", escalation_reason=str(e))
        save_log(domain, log)
        return log
    except hunter.HunterAuthError as e:
        integrations["hunter_blocked"] = f"Hunter setup issue: {e}"
        log.update(status="HUNTER_SETUP_ERROR", escalation_reason=str(e))
        save_log(domain, log)
        return log
    except (hunter.HunterInputError, hunter.HunterTransientError) as e:
        log.update(status="HUNTER_FAILED", escalation_reason=str(e))
        save_log(domain, log)
        return log
    p("scoring contacts")
    log["contact"] = contact_mod.select(domain, company, hdata, log)
    db.execute("DELETE FROM contacts WHERE domain=?", (domain,))
    for c in log["contact"]["candidates"]:
        db.execute("INSERT INTO contacts (domain, name, title, email, role_category, scores_json, total, passed, selected,"
                   " reasons_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (domain, c["name"], c["title"], c["email_research_only"], c["role_category"], json.dumps(c["scores"]),
                    c["total"], int(c["passed"]), int(c.get("selected", False)), json.dumps(c["reasons"])))
    if log["contact"]["status"] != "CONTACT_SELECTED":
        log["status"] = contact_mod.NO_SUITABLE_CONTACT
        log["top_reason"] = log["contact"].get("selection_reason") or log["contact"].get("reason")
        save_log(domain, log)
        return log
    person = log["contact"]["selected"]

    # ---- Pains ----------------------------------------------------------------
    p("pain hypotheses")
    pains = pains_mod.generate(company, person, log["facts"], log["qualification"])
    if pains["status"] != "PAINS_OK" and _extra_research_for_pains(domain, log):
        p("pain evidence insufficient: one more research round")
        save_evidence(domain, log["facts"])
        pains = pains_mod.generate(company, person, log["facts"], log["qualification"])
        pains["after_extra_research"] = True
    log["pains"] = pains
    db.execute("DELETE FROM pains WHERE domain=?", (domain,))
    for i, h in enumerate(pains["hypotheses"]):
        db.execute("INSERT INTO pains (domain, idx, data_json, score, passed, prompt_version) VALUES (?,?,?,?,?,?)",
                   (domain, i, json.dumps(h), h["score"], int(h["passed"]), pains["llm"].get("prompt_version")))
    if pains["status"] != "PAINS_OK":
        log["status"] = pains_mod.INSUFFICIENT_PAIN_EVIDENCE
        log["top_reason"] = "; ".join(pains["fail_reasons"])
        save_log(domain, log)
        return log

    # ---- Email + QA --------------------------------------------------------------
    primary = max(pains["hypotheses"], key=lambda h: h["score"])
    p("drafting email + QA")
    em = email_qa.write_and_qa(company, person, primary, log["facts"])
    em["primary_pain"] = primary["hypothesis"]
    log["email"] = em
    db.execute("DELETE FROM emails WHERE domain=?", (domain,))
    for v in em["versions"]:
        db.execute("INSERT INTO emails (domain, version, subject, body, qa_json, qa_score, passed, created_at) VALUES (?,?,?,?,?,?,?,?)",
                   (domain, v["version"], v["subject"], v["body"], json.dumps({"scores": v["qa_scores"], "checks": v["qa_checks"]}),
                    v["qa_total"], int(v["passed"]), db.now_iso()))
    # ---- Draft for human review (nothing is sent automatically) --------------------------
    # The reviewer edits and sends it from the web app (outreach/records.send_draft).
    if em["status"] == "EMAIL_QA_PASSED":
        final = em["versions"][em["final_version"] - 1]
        log["draft"] = make_draft(final, qa_passed=True)
        log["status"] = DRAFT_READY
        log["top_reason"] = f"Draft ready for review (QA {final['qa_total']}/10)"
    else:
        # Keep the best-scoring version so the reviewer can fix it, clearly flagged as failing QA.
        best = max(em["versions"], key=lambda v: v["qa_total"])
        log["draft"] = make_draft(best, qa_passed=False)
        log["status"] = email_qa.EMAIL_QA_FAILED
        log["top_reason"] = f"Email failed QA after 2 revisions; best draft (QA {best['qa_total']}/10) kept for review"
    p(f"draft ready for review: {log['status']}")
    db.log_activity_safe(domain, "DRAFT_CREATED", {"qa_passed": log["draft"]["qa_passed"], "qa_total": log["draft"]["qa_total"]})
    save_log(domain, log)
    return log


def run_batch(domains: list[str], *, mock_hunter=False, dry_run=False, progress=lambda d, m: None, force=False) -> list[dict]:
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S")
    integrations: dict = {}
    out = []
    for d in [x.split("#")[0].strip() for x in domains]:
        if not d:
            continue
        try:
            out.append(process_domain(d, mock_hunter=mock_hunter, dry_run=dry_run, integrations=integrations,
                                      progress=progress, force=force, run_id=run_id))
        except LLMAuthError as e:
            # Gemini setup problem affects every account: surface it, record it, keep going (each will fail fast).
            log = {"domain": d, "status": "LLM_SETUP_ERROR", "escalation_reason": str(e), "top_reason": str(e)}
            save_log(d, log)
            out.append(log)
        except Exception as e:  # one account's failure must not stop the batch
            log = {"domain": d, "status": "ERROR", "escalation_reason": f"{type(e).__name__}: {e}",
                   "top_reason": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-2000:]}
            save_log(d, log)
            out.append(log)
        progress(d, f"done: {out[-1].get('status')}")
    try:  # every batch run also retries failed log writes and sweeps for passed no-reply deadlines
        from agent.graph import check_deadlines, retry_failed_logging
        lr = retry_failed_logging()
        if lr["spool_written"] or lr["threads_retried"] or lr["still_pending"]:
            progress("activity-log", f"retried failed log writes: {lr}")
        for r in check_deadlines():
            progress(r["thread"], f"no-reply deadline passed -> {r['status']}: {r['reason']}")
    except Exception as e:
        progress("deadline-check", f"failed: {type(e).__name__}: {e}")
    return out
