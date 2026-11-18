"""Persistence helpers shared by the research pipeline (laptop worker) and the web app (Vercel).

Kept free of scraper / research imports so the Vercel function can load it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from config import settings as S
from outreach import send as send_mod
from storage import db


def public_log(log: dict) -> dict:
    return {k: v for k, v in log.items() if not k.startswith("_")}


def save_log(domain: str, log: dict) -> None:
    pub = public_log(log)
    try:  # local copy for inspection; best-effort (read-only filesystem on Vercel)
        S.DECISION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        (S.DECISION_LOG_DIR / f"{domain}.json").write_text(json.dumps(pub, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass
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


def company_name(log: dict) -> str:
    for f in log.get("facts", []):
        if f["field"] == "company_name" and f["verified"]:
            return f["value"]
    return log["domain"].split(".")[0].capitalize()


def open_thread(domain, company, person, subject, text, op, provider_id, dry_run) -> None:
    deadline = (datetime.now(timezone.utc) + timedelta(hours=S.REPLY_DEADLINE_HOURS)).replace(microsecond=0).isoformat()
    from agent.graph import get_graph, get_state, init_thread
    old_state = get_state(domain) if db.query_one("SELECT 1 FROM threads WHERE thread_id=?", (domain,)) else {}
    if old_state and old_state.get("delivery") == "dry-run" and not dry_run:
        # A live send replaces the earlier dry-run thread so replies go through Resend.
        db.execute("DELETE FROM messages WHERE thread_id=?", (domain,))
        db.execute("DELETE FROM threads WHERE thread_id=?", (domain,))
        get_graph().checkpointer.delete_thread(domain)
        db.log_activity_safe(domain, "DRY_RUN_THREAD_REPLACED_BY_LIVE_SEND")
    if not db.query_one("SELECT 1 FROM threads WHERE thread_id=?", (domain,)):
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)",
                   (domain, domain, person["name"], person["title"], company, subject, db.now_iso(), deadline,
                    "AWAITING_REPLY"))
    mid = f"out-{op}"
    db.insert_message(mid, domain, "outbound", "agent", subject, text, provider_id)
    init_thread(domain, deadline, [mid], delivery="dry-run" if dry_run else "resend")


def finalise_reconciled_send(op_row: dict, provider_id: str) -> None:
    """A SEND_UNCERTAIN initial email was found at the provider: record it as sent and open its reply thread."""
    if op_row["kind"] != "initial_email":
        if op_row["kind"].startswith("reply_"):
            db.execute("UPDATE messages SET provider_id=? WHERE message_id=?", (provider_id, f"agent-{op_row['op_id']}"))
        return
    domain = op_row["domain"]
    rec = db.query_one("SELECT decision_log FROM companies WHERE domain=?", (domain,))
    log = json.loads(rec["decision_log"]) if rec and rec["decision_log"] else {}
    person = (log.get("contact") or {}).get("selected")
    if not person:
        return
    dry = op_row["payload"].get("sender") == "dry-run"
    log["status"] = "DRY_RUN_SENT" if dry else "EMAIL_SENT"
    log.setdefault("send", {}).update(status=send_mod.SENT, provider_id=provider_id, reconciled=True)
    log.pop("escalation_reason", None)
    save_log(domain, log)
    open_thread(domain, company_name(log), person, op_row["payload"]["subject"], op_row["payload"]["text"],
                op_row["op_id"], provider_id, dry)


def sender_for_name(name: str | None):
    """Sender used by the reconcile sweep (needs RESEND_API_KEY for resend ops)."""
    if name == "dry-run":
        return None  # dry-run sends can't be uncertain
    try:
        return send_mod.ResendSender()
    except send_mod.SendAuthError:
        return None


def reconcile_sends() -> list[dict]:
    return send_mod.reconcile_uncertain(sender_for_name, finalise_reconciled_send)
