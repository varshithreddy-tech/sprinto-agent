"""Shared test scaffolding: temp DB, fixed clock, fake LLM, dry-run sender, built graph."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from langgraph.checkpoint.sqlite import SqliteSaver

import llm
from agent import calendar_provider as cp
from agent.graph import build_graph
from config import settings as S
from outreach.send import DryRunSender
from storage import db

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)
DEMO = "demo@test.local"


def classify_fake(v: dict) -> dict:
    r = v["reply"].lower()
    base = {"requested_slot": None, "window": None, "prospect_timezone": None, "ambiguity": None, "questions": [],
            "referral": None, "reason": "fake"}
    if "out of office" in r:
        return {**base, "category": "out_of_office"}
    if "unsubscribe" in r or "stop emailing" in r:
        return {**base, "category": "opt_out"}
    if "pricing" in r or "cost" in r:
        return {**base, "category": "pricing_or_security", "questions": ["What is the pricing?"]}
    if "not interested" in r:
        return {**base, "category": "reject"}
    if "talk to" in r:
        return {**base, "category": "referral", "referral": {"name": "Asha Rao", "title": "Head of IT", "email": None}}
    if "reschedule" in r or "move it" in r or "move the meeting" in r:
        m = re.search(r"(\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2})(?: (\w+))?", r)
        slot = {"date": m.group(1), "time": m.group(2), "timezone": m.group(3), "matches_proposed_index": None} if m else None
        return {**base, "category": "reschedule", "requested_slot": slot}
    m = re.search(r"option (\d)", r)
    if m:
        return {**base, "category": "confirm_slot", "requested_slot": {"date": None, "time": None, "timezone": None,
                                                                         "matches_proposed_index": int(m.group(1)) - 1}}
    m = re.search(r"(\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2})(?: (\w+))?", r)
    if m:
        return {**base, "category": "confirm_slot", "requested_slot": {"date": m.group(1), "time": m.group(2),
                                                                         "timezone": m.group(3), "matches_proposed_index": None}}
    if "none of these" in r or "next week" in r:
        return {**base, "category": "counterpropose", "window": {"date_from": None, "date_to": None,
                                                                 "earliest_time": "14:00", "latest_time": None, "timezone": None}}
    if "how does" in r:
        return {**base, "category": "info_request", "questions": ["How does evidence collection work?"]}
    if "interested" in r or "happy to chat" in r:
        return {**base, "category": "interested"}
    return {**base, "category": "ambiguous"}


FAKE_HANDLERS = {
    "reply_classify": classify_fake,
    "reply_draft": lambda v: {"body": f"Hi {v['first_name']},\n\nThanks for the note.\n[[SLOTS]]\n\nBest,\nSprinto Growth Team"},
    "info_answer": lambda v: {"body": f"Hi {v['first_name']},\n\nSprinto automates evidence collection.\n\nBest,\nSprinto Growth Team",
                              "needs_handoff": False, "handoff_reason": None},
}


class Env:
    def __init__(self, tmp_path, monkeypatch, now=NOW):
        self.path = tmp_path / "test.db"
        monkeypatch.setattr(S, "DEMO_EMAIL", DEMO)
        monkeypatch.setattr(S, "DB_PATH", self.path)
        monkeypatch.setattr(S, "DECISION_LOG_DIR", tmp_path / "decision_logs")
        self.conn = db.connect(self.path)
        db.set_db(self.conn)
        self.cal = cp.DemoCalendar(self.conn)
        self.cal.seed(today=now.astimezone(ZoneInfo("Asia/Kolkata")).date())
        cp.set_calendar(self.cal)
        self.fake = llm.FakeLLM(dict(FAKE_HANDLERS))
        llm.set_llm(self.fake)
        self.sender = DryRunSender()
        self.now = now
        self.graph = self.new_graph()

    def new_graph(self):
        cp_conn = sqlite3.connect(str(self.path), check_same_thread=False)
        return build_graph(SqliteSaver(cp_conn), sender_factory=lambda d: self.sender, now=lambda: self.now)

    def open_thread(self, tid="acme.io"):
        from agent.graph import init_thread
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)",
                   (tid, tid, "Priya Testwal", "CISO", "Acme", "SOC 2 evidence at Acme", db.now_iso(),
                    "2026-11-20T04:30:00+00:00", "AWAITING_REPLY"))
        db.execute("INSERT INTO messages (message_id, thread_id, direction, author, subject, body, created_at) VALUES (?,?,?,?,?,?,?)",
                   (f"out-initial-{tid}", tid, "outbound", "agent", "SOC 2 evidence at Acme", "Hi Priya, ...", db.now_iso()))
        init_thread(tid, "2026-11-20T04:30:00+00:00", [f"out-initial-{tid}"], delivery="dry-run", graph=self.graph)
        return tid

    def reply(self, tid, text, mid=None):
        from agent.graph import process_inbound
        return process_inbound(tid, text, graph=self.graph, message_id=mid)

    def close(self):
        db.set_db(None)
        cp.set_calendar(None)
        llm.set_llm(None)
