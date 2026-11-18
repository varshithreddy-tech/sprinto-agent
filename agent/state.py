"""Persistent per-prospect reply/scheduling state (LangGraph checkpointed, one thread per prospect)."""
from __future__ import annotations

from typing import Any, Optional, TypedDict

# reply_status values
AWAITING_REPLY = "AWAITING_REPLY"
TIMES_PROPOSED = "TIMES_PROPOSED"
INFO_SENT = "INFO_SENT"
CLARIFICATION_REQUESTED = "CLARIFICATION_REQUESTED"
MEETING_BOOKED = "MEETING_BOOKED"
REJECTED = "REJECTED"
OPTED_OUT = "OPTED_OUT"
ESCALATED = "ESCALATED"
OUT_OF_OFFICE = "OUT_OF_OFFICE"
DELIVERY_NOTICE = "DELIVERY_NOTICE"
BOOKING_UNCERTAIN = "BOOKING_UNCERTAIN"

STOPPED = {REJECTED, OPTED_OUT, ESCALATED, BOOKING_UNCERTAIN}
# Statuses where the agent is waiting on the prospect, so the no-reply deadline applies.
# MEETING_BOOKED is not waiting: a booked meeting needs no reply.
WAITING = {AWAITING_REPLY, TIMES_PROPOSED, INFO_SENT, CLARIFICATION_REQUESTED, OUT_OF_OFFICE}


class ThreadState(TypedDict, total=False):
    thread_id: str
    # rubric 3.10 / Part 6 fields
    reply_status: str
    reply_round: int
    calendar_checked: bool
    checked_at: Optional[str]
    checked_slots: list[str]          # free slots found by the latest calendar check
    times_proposed: list[str]         # every slot ever proposed (ISO, organizer tz)
    last_proposed: list[str]          # the latest batch offered; "option N" / "the first one" refer to this
    rejected_slots: list[str]
    requested_slot: Optional[str]
    confirmed_slot: Optional[str]
    prospect_tz: Optional[str]        # only when the prospect states it explicitly
    organizer_tz: str
    meeting_status: str               # NONE | PROPOSED | CONFIRMED | RESCHEDULE_REQUESTED | RESCHEDULED | UNCERTAIN
    event_id: Optional[str]
    event_sequence: int               # ICS SEQUENCE; +1 on every reschedule
    crm_logged: bool
    reply_deadline: Optional[str]
    retry_counts: dict[str, int]
    suppressed: bool
    escalation_reason: Optional[str]
    processed_message_ids: list[str]
    agent_message_ids: list[str]
    delivery: str                     # "resend" | "dry-run"
    referral: Optional[dict]
    # per-invocation scratch
    incoming: Optional[dict]
    classification: Optional[dict]
    outbox: list[dict]
    pending_log: list[dict]
    skip_reason: Optional[str]
    window: Optional[dict]


def initial_state(thread_id: str, deadline: str, organizer_tz: str, agent_message_ids: list[str], delivery: str) -> dict[str, Any]:
    return {
        "thread_id": thread_id, "reply_status": AWAITING_REPLY, "reply_round": 0, "calendar_checked": False,
        "checked_at": None, "checked_slots": [], "times_proposed": [], "last_proposed": [], "rejected_slots": [], "requested_slot": None,
        "confirmed_slot": None, "prospect_tz": None, "organizer_tz": organizer_tz, "meeting_status": "NONE",
        "event_id": None, "event_sequence": 0, "crm_logged": True, "reply_deadline": deadline, "retry_counts": {}, "suppressed": False,
        "escalation_reason": None, "processed_message_ids": [], "agent_message_ids": list(agent_message_ids),
        "delivery": delivery, "referral": None, "incoming": None, "classification": None, "outbox": [],
        "pending_log": [], "skip_reason": None, "window": None,
    }
