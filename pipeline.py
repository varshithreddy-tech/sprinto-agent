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

TERMINAL_SENT = {"EMAIL_SENT", "DRY_RUN_SENT"}


def _public_log(log: dict) -> dict:
    return {k: v for k, v in log.items() if not k.startswith("_")}


def save_log(domain: str, log: dict) -> None:
    S.DECISION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    pub = _public_log(log)
    (S.DECISION_LOG_DIR / f"{domain}.json").write_text(json.dumps(pub, indent=2, default=str), encoding="utf-8")
    q = log.get("qualification") or {}
    c = (log.get("contact") or {}).get("selected") or {}
    db.upsert_company(domain, status=log.get("status"), total=q.get("total"), rounds=log.get("additional_rounds", 0),
                      pages_fetched=log.get("pages_fetched", 0), contact_name=c.get("name"), contact_title=c.get("title"),
                      email_status=(log.get("send") or {}).get("status") or (log.get("email") or {}).get("status"),
                      top_reason=log.get("top_reason"), rubric_version=S.RUBRIC_VERSION,
                      escalation_reason=log.get("escalation_reason"),
                      decision_log=json.dumps(pub, default=str), run_id=log.get("run_id"))


def save_evidence(domain: str, facts: list[dict]) -> None:
    db.execute("DELETE FROM evidence WHERE domain=?", (domain,))
    for f in facts:
        db.execute("INSERT INTO evidence (domain, field, value, label, source_url, exact_excerpt, retrieval_date,"
                   " publication_date, verified, round) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (domain, f["field"], f["value"], f["label"], f["source_url"], f["exact_excerpt"], f.get("retrieval_date"),
                    f.get("publication_date"), int(f["verified"]), f.get("round", 0)))


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


def company_name(log: dict) -> str:
    for f in log.get("facts", []):
        if f["field"] == "company_name" and f["verified"]:
            return f["value"]
    return log["domain"].split(".")[0].capitalize()


def process_domain(domain: str, *, mock_hunter: bool = False, dry_run: bool = False, integrations: dict | None = None,
                   progress=lambda d, m: None, force: bool = False, run_id: str = "") -> dict:
    integrations = integrations if integrations is not None else {}
    domain = domain.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
    existing = db.query_one("SELECT status FROM companies WHERE domain=?", (domain,))
    # Resume: skip domains already sent in the same mode (a dry-run send never blocks a live run).
    already = "DRY_RUN_SENT" if dry_run else "EMAIL_SENT"
    if existing and existing["status"] in ({already, "EMAIL_SENT"} if dry_run else {already}) and not force:
        progress(domain, f"already {existing['status']} — skipped (resume)")
        return db.query_one("SELECT * FROM companies WHERE domain=?", (domain,))
    p = lambda m: progress(domain, m)
    log = research_company(domain, progress=p)
    log["run_id"] = run_id
    log["mode"] = {"mock_hunter": mock_hunter, "dry_run": dry_run}
    save_evidence(domain, log["facts"])
    save_log(domain, log)
    db.log_activity(domain, "QUALIFICATION", {"status": log["status"], "total": (log.get("qualification") or {}).get("total")})
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
    if em["status"] != "EMAIL_QA_PASSED":
        log["status"] = email_qa.EMAIL_QA_FAILED
        log["top_reason"] = "Email failed QA after 2 revisions"
        save_log(domain, log)
        return log
    final = em["versions"][em["final_version"] - 1]

    # ---- Send ---------------------------------------------------------------------
    try:
        sender = send_mod.get_sender(dry_run)
    except send_mod.SendAuthError as e:
        log.update(status="SEND_SETUP_ERROR", escalation_reason=str(e))
        save_log(domain, log)
        return log
    op = send_mod.op_id(domain, "initial-email", em["final_version"], sender.name)
    banner = send_mod.demo_banner(person["name"], person["title"], company)
    text = f"{banner}\n\n{final['body']}"
    p("sending" + (" (dry run)" if dry_run else f" to {S.DEMO_EMAIL}"))
    try:
        res = send_mod.send_once(op, "initial_email", domain, final["subject"], text, sender)
    except send_mod.RecipientGuardError as e:
        res = {"status": send_mod.FAILED, "error": str(e), "op_id": op}
    log["send"] = {**res, "subject": final["subject"], "to": S.DEMO_EMAIL, "sender": sender.name,
                   "intended_for": f"{person['name']} <research-only: {person.get('email_research_only')}>"}
    if res["status"] in (send_mod.SENT, send_mod.DUPLICATE):
        log["status"] = "DRY_RUN_SENT" if dry_run else "EMAIL_SENT"
        _open_thread(domain, company, person, final["subject"], text, op, res.get("provider_id"), dry_run)
    else:
        log["status"] = res["status"]
        log["escalation_reason"] = res.get("error")
    log["top_reason"] = log.get("top_reason")
    db.log_activity(domain, "EMAIL_" + res["status"], {"op_id": op, "provider_id": res.get("provider_id"), "dry_run": dry_run})
    save_log(domain, log)
    return log


def _open_thread(domain, company, person, subject, text, op, provider_id, dry_run) -> None:
    deadline = (datetime.now(timezone.utc) + timedelta(hours=S.REPLY_DEADLINE_HOURS)).replace(microsecond=0).isoformat()
    from agent.graph import get_graph, get_state, init_thread
    old_state = get_state(domain) if db.query_one("SELECT 1 FROM threads WHERE thread_id=?", (domain,)) else {}
    if old_state and old_state.get("delivery") == "dry-run" and not dry_run:
        # A live send replaces the earlier dry-run thread so replies go through Resend.
        db.execute("DELETE FROM messages WHERE thread_id=?", (domain,))
        db.execute("DELETE FROM threads WHERE thread_id=?", (domain,))
        get_graph().checkpointer.delete_thread(domain)
        db.log_activity(domain, "DRY_RUN_THREAD_REPLACED_BY_LIVE_SEND")
    if not db.query_one("SELECT 1 FROM threads WHERE thread_id=?", (domain,)):
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)",
                   (domain, domain, person["name"], person["title"], company, subject, db.now_iso(), deadline,
                    "AWAITING_REPLY"))
    mid = f"out-{op}"
    db.execute("INSERT OR IGNORE INTO messages (message_id, thread_id, direction, author, subject, body, provider_id, created_at)"
               " VALUES (?,?,?,?,?,?,?,?)", (mid, domain, "outbound", "agent", subject, text, provider_id, db.now_iso()))
    init_thread(domain, deadline, [mid], delivery="dry-run" if dry_run else "resend")


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
    try:  # every batch run also sweeps existing threads for passed no-reply deadlines
        from agent.graph import check_deadlines
        for r in check_deadlines():
            progress(r["thread"], f"no-reply deadline passed -> {r['status']}: {r['reason']}")
    except Exception as e:
        progress("deadline-check", f"failed: {type(e).__name__}: {e}")
    return out
