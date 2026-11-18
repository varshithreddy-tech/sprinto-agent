"""LangGraph reply/scheduling agent with SQLite checkpoints (one LangGraph thread per prospect).

Edges follow rubric 3.8. Only tracked threads and new prospect messages are processed; agent-generated
messages are ignored by stored message IDs to prevent reply loops.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from agent import state as st
from agent import tools as T
from agent.calendar_provider import CalendarError, fmt_slot
from config import settings as S
from outreach import send as send_mod
from storage import db

Now = Callable[[], datetime]


def _thread(thread_id: str) -> dict:
    t = db.query_one("SELECT * FROM threads WHERE thread_id=?", (thread_id,))
    if not t:
        raise KeyError(f"untracked thread {thread_id}")
    return t


def _last_agent_message(thread_id: str) -> str:
    r = db.query_one("SELECT body FROM messages WHERE thread_id=? AND direction='outbound' ORDER BY created_at DESC, rowid DESC LIMIT 1",
                     (thread_id,))
    return r["body"] if r else ""


def build_graph(checkpointer, sender_factory: Callable[[str], object] | None = None, now: Now | None = None):
    now = now or (lambda: datetime.now(timezone.utc))
    sender_factory = sender_factory or (lambda delivery: send_mod.get_sender(delivery == "dry-run"))

    def entry(event, detail=None):
        return db.new_entry(event, detail, ts=now().replace(microsecond=0).isoformat())

    def log_entry(state, event, detail=None):
        return [*state.get("pending_log", []), entry(event, detail)]

    # ------------------------------------------------------------------ nodes
    def ingest(state: st.ThreadState) -> dict:
        msg = state.get("incoming") or {}
        # Entries a previous run failed to write are KEPT (never wiped) until update_activity_log succeeds.
        carried = list(state.get("pending_log") or [])
        base = {"outbox": [], "pending_log": carried, "skip_reason": None, "classification": None, "window": None}
        if msg.get("type") == "init":
            return {**st.initial_state(msg["thread_id"], msg["deadline"], S.ORGANIZER_TZ, msg.get("agent_message_ids", []),
                                       msg.get("delivery", "resend")), **base, "incoming": msg,
                    "skip_reason": "thread initialised",
                    "pending_log": [entry("THREAD_OPENED", {"reply_deadline": msg["deadline"]})]}
        if msg.get("type") == "log_retry":
            return {**base, "skip_reason": "retrying failed activity-log writes" if carried else "nothing to retry"}
        if msg.get("type") == "deadline_tick":
            dl = state.get("reply_deadline")
            waiting = state.get("reply_status") in st.WAITING
            at = datetime.fromisoformat(msg["now"]) if msg.get("now") else now()  # checker's clock wins
            if waiting and dl and at > datetime.fromisoformat(dl) and not state.get("suppressed"):
                return {**base, "skip_reason": None, "classification": {"category": "no_reply"}}
            return {**base, "skip_reason": "deadline not reached or thread not waiting"}
        mid = msg.get("message_id")
        if not mid:
            return {**base, "skip_reason": "message without id"}
        if mid in state.get("processed_message_ids", []):
            return {**base, "skip_reason": f"already processed {mid}"}
        if mid in state.get("agent_message_ids", []) or msg.get("author") == "agent":
            return {**base, "skip_reason": f"agent-generated message {mid} ignored (loop prevention)"}
        processed = [*state.get("processed_message_ids", []), mid]
        if state.get("suppressed"):
            return {**base, "processed_message_ids": processed, "skip_reason": "thread suppressed (opt-out)"}
        if state.get("reply_status") in st.STOPPED:
            return {**base, "processed_message_ids": processed,
                    "skip_reason": f"thread paused ({state.get('reply_status')}); human handles further replies",
                    "pending_log": carried + [entry("MESSAGE_WHILE_PAUSED", {"message_id": mid})]}
        return {**base, "processed_message_ids": processed, "escalation_reason": None}

    def classify_reply(state: st.ThreadState) -> dict:
        msg = state["incoming"]
        booked = state.get("confirmed_slot") if state.get("event_id") else None
        latest = state.get("last_proposed") or state.get("times_proposed", [])[-3:]
        c, meta = T.classify_reply(msg["body"], latest, _last_agent_message(state["thread_id"]), now(), booked)
        cd = c.model_dump()
        upd = {"classification": cd, "window": c.window}
        ptz = T.resolve_tz(c.prospect_timezone)
        if ptz:
            upd["prospect_tz"] = ptz  # only when explicitly stated
        upd["pending_log"] = log_entry(state, "REPLY_CLASSIFIED", {"category": c.category, "reason": c.reason,
                                                                    "prompt": meta.get("prompt_version")})
        return upd

    def check_calendar(state: st.ThreadState) -> dict:
        rc = dict(state.get("retry_counts") or {})
        try:
            res = T.check_calendar(state, now(), state.get("window"))
        except CalendarError as e:
            rc["calendar"] = rc.get("calendar", 0) + S.MAX_FETCH_ATTEMPTS
            return {"retry_counts": rc, "calendar_checked": False, "checked_slots": [],
                    "escalation_reason": f"Calendar check failed: {e}"}
        return {"calendar_checked": True, "checked_at": res["checked_at"], "checked_slots": res["slots"], "retry_counts": rc,
                "pending_log": log_entry(state, "CALENDAR_CHECKED", {"free_slots": res["slots"]})}

    def propose_times(state: st.ThreadState) -> dict:
        slots = state.get("checked_slots", [])
        if not state.get("calendar_checked") or not slots:
            return {"escalation_reason": state.get("escalation_reason") or "No calendar-checked free slots available"}
        extra = ""
        c = state.get("classification") or {}
        moving = state.get("meeting_status") == "RESCHEDULE_REQUESTED"
        if moving:
            extra = ("They asked to move the meeting that is currently booked. Acknowledge that, say the current booking stays "
                     "in place until they pick a new time, and offer these new times. ")
        if c.get("requested_slot") and (c["requested_slot"].get("date") or c["requested_slot"].get("matches_proposed_index") is not None):
            extra += "Explain briefly that the time they asked for is not available, then offer these instead."
        item = T.propose_times(slots, _thread(state["thread_id"]), state["incoming"]["body"], state.get("prospect_tz"), extra)
        return {"outbox": [*state.get("outbox", []), item], "times_proposed": [*state.get("times_proposed", []), *slots],
                "last_proposed": list(slots),
                "reply_status": st.TIMES_PROPOSED, "meeting_status": "RESCHEDULE_REQUESTED" if moving else "PROPOSED",
                "pending_log": log_entry(state, "TIMES_PROPOSED", {"slots": [fmt_slot(s) for s in slots]})}

    def answer_info_request(state: st.ThreadState) -> dict:
        from agent.tools import Classification
        ans = T.answer_info_request(Classification(**state["classification"]), _thread(state["thread_id"]), state["incoming"]["body"])
        if ans.needs_handoff:
            return {"escalation_reason": f"Info request needs a human: {ans.handoff_reason}"}
        return {"outbox": [*state.get("outbox", []), {"kind": "info", "body": ans.body}], "reply_status": st.INFO_SENT,
                "pending_log": log_entry(state, "INFO_ANSWERED", {"questions": state["classification"].get("questions")})}

    def resolve_confirmation(state: st.ThreadState) -> dict:
        from agent.tools import Classification
        slot, problem = T.resolve_slot(Classification(**state["classification"]), state)
        if problem:
            item = T.clarify(problem, _thread(state["thread_id"]), state["incoming"]["body"])
            return {"requested_slot": None, "outbox": [*state.get("outbox", []), item],
                    "reply_status": st.CLARIFICATION_REQUESTED,
                    "pending_log": log_entry(state, "CLARIFICATION_REQUESTED", {"problem": problem})}
        return {"requested_slot": slot}

    def book_meeting(state: st.ThreadState) -> dict:
        slot = state["requested_slot"]
        thread = _thread(state["thread_id"])
        res = T.book_meeting(state, slot, thread, now())
        logs = log_entry(state, "BOOKING_ATTEMPT", res)
        if res["status"] == "BOOKED":
            other = [s for s in state.get("times_proposed", []) if s != slot]
            seq = int(state.get("event_sequence") or 0) + (1 if res["kind"] == "RESCHEDULED" else 0)
            sent_ics = T.build_ics(res["event_id"], slot, res["title"], S.DEMO_EMAIL,
                                   f"DEMO meeting for {thread['contact_name']} ({thread['company']}). 30 minutes.",
                                   sequence=seq)
            body = T._draft(f"Confirm the meeting is booked for [[SLOTS]] (event created; invite attached). "
                            f"{'This replaces the previous time.' if res['kind'] == 'RESCHEDULED' else ''}",
                            thread, state["incoming"]["body"])
            line = fmt_slot(slot, state.get("prospect_tz"))
            body = body.replace("[[SLOTS]]", line) if "[[SLOTS]]" in body else body.rstrip() + f"\n\nBooked: {line}"
            return {"confirmed_slot": slot, "event_id": res["event_id"], "meeting_status": res["kind"], "event_sequence": seq,
                    "reply_status": st.MEETING_BOOKED,
                    "rejected_slots": sorted(set(state.get("rejected_slots", [])) | set(other)),
                    "outbox": [*state.get("outbox", []), {"kind": "booking_confirmation", "body": body, "ics": sent_ics}],
                    "pending_log": logs + [entry("MEETING_" + res["kind"], {"event_id": res["event_id"], "slot": line})]}
        if res["status"] == "UNCERTAIN":
            return {"meeting_status": "UNCERTAIN", "reply_status": st.BOOKING_UNCERTAIN, "pending_log": logs,
                    "escalation_reason": f"BOOKING_UNCERTAIN: {res.get('reason')}"}
        # BUSY or INVALID -> invalidate, reject that slot, propose alternatives
        return {"calendar_checked": False, "pending_log": logs,
                "rejected_slots": sorted(set(state.get("rejected_slots", [])) | {slot}) if res["status"] == "BUSY" else state.get("rejected_slots", [])}

    def counterproposal(state: st.ThreadState) -> dict:
        rnd = int(state.get("reply_round", 0)) + 1
        rejected = sorted(set(state.get("rejected_slots", [])) | set(state.get("times_proposed", [])))
        upd = {"reply_round": rnd, "calendar_checked": False, "checked_at": None, "checked_slots": [],
               "rejected_slots": rejected,
               "pending_log": log_entry(state, "COUNTERPROPOSAL", {"reply_round": rnd, "window": state.get("window")})}
        if rnd >= S.MAX_COUNTERPROPOSAL_ROUNDS:
            upd["escalation_reason"] = f"{rnd} counterproposal rounds without confirmation"
        return upd

    def reschedule(state: st.ThreadState) -> dict:
        if not state.get("event_id"):
            # Nothing booked yet: this is an ordinary counterproposal.
            upd = counterproposal(state)
            upd["pending_log"] = upd["pending_log"] + [entry("RESCHEDULE_WITHOUT_BOOKING_TREATED_AS_COUNTERPROPOSAL")]
            return upd
        old = state.get("confirmed_slot")
        # A booked meeting starts a fresh negotiation: counterproposal rounds reset. The existing event is kept
        # until a new slot is confirmed and then UPDATED (same event id), never deleted and re-created.
        return {"reply_round": 0, "calendar_checked": False, "checked_at": None, "checked_slots": [],
                "meeting_status": "RESCHEDULE_REQUESTED",
                "rejected_slots": sorted(set(state.get("rejected_slots", [])) | ({old} if old else set())),
                "pending_log": log_entry(state, "RESCHEDULE_REQUESTED", {"event_id": state.get("event_id"),
                                                                          "current_slot": fmt_slot(old) if old else None})}

    def stop_outreach(state: st.ThreadState) -> dict:
        return {"reply_status": st.REJECTED, "pending_log": log_entry(state, "REJECTED_STOP_OUTREACH")}

    def suppress(state: st.ThreadState) -> dict:
        return {"reply_status": st.OPTED_OUT, "suppressed": True, "pending_log": log_entry(state, "OPTED_OUT_SUPPRESSED")}

    def record_referral(state: st.ThreadState) -> dict:
        ref = (state.get("classification") or {}).get("referral") or {}
        return {"referral": ref, "escalation_reason": f"Referral recorded ({ref.get('name') or 'unnamed'}"
                f"{', ' + ref['title'] if ref.get('title') else ''}); identity/contact must be verified via Hunter before outreach",
                "pending_log": log_entry(state, "REFERRAL_RECORDED", ref)}

    def auto_message(state: st.ThreadState) -> dict:
        cat = state["classification"]["category"]
        status = st.OUT_OF_OFFICE if cat == "out_of_office" else st.DELIVERY_NOTICE
        upd = {"pending_log": log_entry(state, status, {"note": "classified separately from human interest"})}
        if cat == "out_of_office" and state.get("reply_status") == st.AWAITING_REPLY:
            upd["reply_status"] = st.OUT_OF_OFFICE
        if cat == "delivery_notice":
            upd["escalation_reason"] = "Delivery notice received (bounce)"
        return upd

    def escalate(state: st.ThreadState) -> dict:
        c = (state.get("classification") or {})
        reason = state.get("escalation_reason")
        if not reason:
            cat = c.get("category")
            reason = {"pricing_or_security": "Pricing/security request needs a human",
                      "ambiguous": f"Ambiguous reply: {c.get('reason', '')}",
                      "no_reply": f"No reply within {S.REPLY_DEADLINE_HOURS}h deadline"}.get(cat, f"Handoff: {cat}")
        status = state.get("reply_status") if state.get("reply_status") == st.BOOKING_UNCERTAIN else st.ESCALATED
        return {"reply_status": status, "escalation_reason": reason,
                "pending_log": log_entry(state, "ESCALATED", {"reason": reason, "paused": "this prospect only"})}

    def send_replies(state: st.ThreadState) -> dict:
        outbox = state.get("outbox", [])
        if not outbox:
            return {}
        thread = _thread(state["thread_id"])
        sender = sender_factory(state.get("delivery", "resend"))
        agent_ids = list(state.get("agent_message_ids", []))
        logs = list(state.get("pending_log", []))
        in_id = (state.get("incoming") or {}).get("message_id", "tick")
        upd: dict = {}
        for item in outbox:
            op = send_mod.op_id(state["thread_id"], in_id, item["kind"])
            subject = f"Re: {thread['subject']}"  # genuine reply in an existing thread
            text = f"DEMO — reply-agent response intended for {thread['contact_name']}, {thread['contact_title']} at {thread['company']}\n\n{item['body']}"
            att = None
            if item.get("ics"):
                att = [{"filename": "invite.ics", "content": list(item["ics"].encode()), "content_type": "text/calendar"}]
            res = send_mod.send_once(op, "reply_" + item["kind"], state["thread_id"], subject, text, sender, attachments=att)
            mid = f"agent-{op}"
            agent_ids.append(mid)
            db.execute("INSERT OR IGNORE INTO messages (message_id, thread_id, direction, author, subject, body, provider_id, created_at)"
                       " VALUES (?,?,?,?,?,?,?,?)", (mid, state["thread_id"], "outbound", "agent", subject,
                                                     text + ("\n\n[invite.ics attached]" if att else ""), res.get("provider_id"), db.now_iso()))
            logs.append(entry("REPLY_" + res["status"], {"kind": item["kind"], "op_id": op, "provider_id": res.get("provider_id")}))
            if res["status"] not in (send_mod.SENT, send_mod.DUPLICATE):
                upd["escalation_reason"] = f"SEND_UNCERTAIN for {item['kind']}: {res.get('error')}"
                upd["reply_status"] = st.ESCALATED
        deadline = (now() + timedelta(hours=S.REPLY_DEADLINE_HOURS)).replace(microsecond=0).isoformat()
        db.execute("UPDATE threads SET reply_deadline=? WHERE thread_id=?", (deadline, state["thread_id"]))
        upd.update({"agent_message_ids": agent_ids, "outbox": [], "pending_log": logs, "reply_deadline": deadline})
        return upd

    def update_activity_log(state: st.ThreadState) -> dict:
        try:  # mirror status for the dashboard; independent of the log write
            db.execute("UPDATE threads SET status=? WHERE thread_id=?", (state.get("reply_status"), state["thread_id"]))
        except Exception:
            pass
        entries = state.get("pending_log", [])
        if not entries:
            return {}
        ok = T.update_activity_log(state["thread_id"], entries)
        rc = dict(state.get("retry_counts") or {})
        if not ok:
            # Actions already taken (sends, bookings) stand. Only the log write is retried: the entries stay in
            # the checkpoint (crm_logged=False) and retry_failed_logging() picks them up on the next sweep.
            rc["logging"] = rc.get("logging", 0) + 1
            return {"crm_logged": False, "retry_counts": rc}
        return {"crm_logged": True, "pending_log": []}

    # ------------------------------------------------------------------ routing
    def after_ingest(state) -> str:
        if state.get("skip_reason"):
            return "update_activity_log"
        if (state.get("incoming") or {}).get("type") == "init":
            return "update_activity_log"
        if (state.get("classification") or {}).get("category") == "no_reply":
            return "escalate"
        return "classify_reply"

    def after_classify(state) -> str:
        cat = (state.get("classification") or {}).get("category")
        return {"interested": "check_calendar", "info_request": "answer_info_request", "confirm_slot": "resolve_confirmation",
                "counterpropose": "counterproposal", "reschedule": "reschedule", "reject": "stop_outreach", "opt_out": "suppress",
                "referral": "record_referral", "out_of_office": "auto_message", "delivery_notice": "auto_message",
                }.get(cat, "escalate")

    def after_check(state) -> str:
        return "escalate" if not state.get("calendar_checked") else "propose_times"

    def after_propose(state) -> str:
        return "escalate" if state.get("escalation_reason") and not state.get("outbox") else "send_replies"

    def after_info(state) -> str:
        return "escalate" if state.get("escalation_reason") else "send_replies"

    def after_resolve(state) -> str:
        return "send_replies" if not state.get("requested_slot") else "book_meeting"

    def after_book(state) -> str:
        if state.get("reply_status") == st.BOOKING_UNCERTAIN:
            return "escalate"
        if state.get("reply_status") == st.MEETING_BOOKED and state.get("confirmed_slot") == state.get("requested_slot") \
                and state.get("outbox"):
            return "send_replies"
        return "check_calendar"  # busy/invalid -> propose alternatives

    def after_counter(state) -> str:
        return "escalate" if state.get("escalation_reason") else "check_calendar"

    def after_reschedule(state) -> str:
        if state.get("escalation_reason"):
            return "escalate"
        rs = (state.get("classification") or {}).get("requested_slot") or {}
        if state.get("event_id") and (rs.get("date") or rs.get("matches_proposed_index") is not None):
            return "resolve_confirmation"  # exact new slot -> fresh check -> update the same event
        return "check_calendar"

    def after_auto(state) -> str:
        return "escalate" if state.get("escalation_reason") else "update_activity_log"

    g = StateGraph(st.ThreadState)
    for name, fn in [("ingest", ingest), ("classify_reply", classify_reply), ("check_calendar", check_calendar),
                     ("propose_times", propose_times), ("answer_info_request", answer_info_request),
                     ("resolve_confirmation", resolve_confirmation), ("book_meeting", book_meeting),
                     ("counterproposal", counterproposal), ("reschedule", reschedule), ("stop_outreach", stop_outreach), ("suppress", suppress),
                     ("record_referral", record_referral), ("auto_message", auto_message), ("escalate", escalate),
                     ("send_replies", send_replies), ("update_activity_log", update_activity_log)]:
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    g.add_conditional_edges("ingest", after_ingest)
    g.add_conditional_edges("classify_reply", after_classify)
    g.add_conditional_edges("check_calendar", after_check)
    g.add_conditional_edges("propose_times", after_propose)
    g.add_conditional_edges("answer_info_request", after_info)
    g.add_conditional_edges("resolve_confirmation", after_resolve)
    g.add_conditional_edges("book_meeting", after_book)
    g.add_conditional_edges("counterproposal", after_counter)
    g.add_conditional_edges("reschedule", after_reschedule)
    g.add_conditional_edges("auto_message", after_auto)
    g.add_conditional_edges("record_referral", lambda s: "escalate")
    for n in ("stop_outreach", "suppress"):
        g.add_edge(n, "update_activity_log")
    g.add_edge("escalate", "update_activity_log")
    g.add_edge("send_replies", "update_activity_log")
    g.add_edge("update_activity_log", END)
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------- runtime API
_graph = None
_cp_conn: sqlite3.Connection | None = None


def get_graph():
    global _graph, _cp_conn
    if _graph is None:
        _cp_conn = sqlite3.connect(str(S.DB_PATH), check_same_thread=False)
        _graph = build_graph(SqliteSaver(_cp_conn))
    return _graph


def set_graph(g) -> None:
    global _graph
    _graph = g


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def init_thread(thread_id: str, deadline: str, agent_message_ids: list[str], delivery: str | None = None, graph=None) -> dict:
    g = graph or get_graph()
    if g.get_state(_cfg(thread_id)).values.get("thread_id"):
        return g.get_state(_cfg(thread_id)).values  # already initialised (resume-safe)
    if delivery is None:
        dry = any("dryrun-" in (r.get("provider_id") or "") for r in db.query(
            "SELECT provider_id FROM messages WHERE thread_id=?", (thread_id,)))
        delivery = "dry-run" if dry else "resend"
    return g.invoke({"incoming": {"type": "init", "thread_id": thread_id, "deadline": deadline,
                                  "agent_message_ids": agent_message_ids, "delivery": delivery}}, _cfg(thread_id))


def get_state(thread_id: str, graph=None) -> dict:
    return (graph or get_graph()).get_state(_cfg(thread_id)).values


def process_inbound(thread_id: str, body: str, simulated: bool = True, author: str = "prospect", graph=None,
                    message_id: str | None = None) -> dict:
    """Store an inbound message on a tracked thread and run the reply agent on it."""
    _thread(thread_id)  # only tracked threads
    mid = message_id or send_mod.new_message_id()
    db.execute("INSERT OR IGNORE INTO messages (message_id, thread_id, direction, author, subject, body, provider_id, created_at, simulated)"
               " VALUES (?,?,?,?,?,?,?,?,?)", (mid, thread_id, "inbound", author, None, body, None, db.now_iso(), int(simulated)))
    return (graph or get_graph()).invoke({"incoming": {"message_id": mid, "body": body, "author": author}}, _cfg(thread_id))


LAST_DEADLINE_CHECK: dict = {"at": None, "escalated": [], "error": None}


def due_threads(now: datetime, graph=None) -> list[str]:
    """Threads waiting on the prospect whose no-reply deadline has passed. Read-only."""
    due = []
    for t in db.query("SELECT thread_id FROM threads ORDER BY thread_id"):
        s = get_state(t["thread_id"], graph)
        dl = s.get("reply_deadline") if s else None
        if (s and s.get("reply_status") in st.WAITING and not s.get("suppressed") and dl
                and now > datetime.fromisoformat(dl)):
            due.append(t["thread_id"])
    return due


def check_deadlines(graph=None, now: datetime | None = None) -> list[dict]:
    """Escalate (pause) every thread past its no-reply deadline. Idempotent: an escalated thread is no
    longer 'waiting', so re-running does nothing. Only due threads are invoked, so routine checks add
    no checkpoints. One thread's failure doesn't stop the others."""
    now = now or datetime.now(timezone.utc)
    out, errors = [], []
    for tid in due_threads(now, graph):
        try:
            after = (graph or get_graph()).invoke({"incoming": {"type": "deadline_tick", "now": now.isoformat()}}, _cfg(tid))
            out.append({"thread": tid, "status": after.get("reply_status"), "reason": after.get("escalation_reason")})
        except Exception as e:  # keep going for other threads
            errors.append(f"{tid}: {type(e).__name__}: {e}")
            db.log_activity_safe(tid, "DEADLINE_CHECK_FAILED", str(e))
    LAST_DEADLINE_CHECK.update(at=now.isoformat(), escalated=out, error="; ".join(errors) or None)
    return out


LAST_RECONCILE: dict = {"at": None, "results": [], "error": None}
LAST_LOG_RETRY: dict = {"at": None, "spool_written": 0, "threads_retried": [], "still_pending": 0}


def threads_with_unlogged_entries(graph=None) -> list[str]:
    out = []
    for t in db.query("SELECT thread_id FROM threads ORDER BY thread_id"):
        s = get_state(t["thread_id"], graph)
        if s and s.get("pending_log") and not s.get("crm_logged", True):
            out.append(t["thread_id"])
    return out


def retry_failed_logging(graph=None) -> dict:
    """Retry ONLY the logging that failed earlier: spooled pipeline entries + entries kept in thread checkpoints.
    Never re-runs sends or bookings. Entry ids make it safe to run any number of times."""
    spool = db.flush_spool()
    retried, still = [], 0
    for tid in threads_with_unlogged_entries(graph):
        try:
            after = (graph or get_graph()).invoke({"incoming": {"type": "log_retry"}}, _cfg(tid))
            if after.get("crm_logged"):
                retried.append(tid)
            else:
                still += len(after.get("pending_log") or [])
        except Exception:
            still += 1
    res = {"at": datetime.now(timezone.utc).isoformat(), "spool_written": spool["written"],
           "threads_retried": retried, "still_pending": still + spool["remaining"]}
    LAST_LOG_RETRY.update(res)
    return res


def pending_log_counts(graph=None) -> dict:
    return {"spooled": db.spool_count(), "threads": threads_with_unlogged_entries(graph)}


def start_deadline_watcher(interval_minutes: float | None = None) -> threading.Thread:
    """Background thread that, every N minutes for as long as the process lives, retries failed activity-log
    writes and escalates threads past their no-reply deadline (used by the dashboard and `run.py --watch`)."""
    interval = 60 * (interval_minutes if interval_minutes is not None else S.DEADLINE_CHECK_MINUTES)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                retry_failed_logging()
            except Exception:
                pass
            try:
                from pipeline import reconcile_sends
                LAST_RECONCILE.update(at=datetime.now(timezone.utc).isoformat(), results=reconcile_sends())
            except Exception as e:
                LAST_RECONCILE.update(at=datetime.now(timezone.utc).isoformat(), error=str(e))
            try:
                check_deadlines()
            except Exception as e:  # never let the watcher die
                LAST_DEADLINE_CHECK.update(at=datetime.now(timezone.utc).isoformat(), error=str(e))
            stop.wait(interval)
    t = threading.Thread(target=loop, name="deadline-watcher", daemon=True)
    t.stop_event = stop  # set() to stop the watcher
    t.start()
    return t
