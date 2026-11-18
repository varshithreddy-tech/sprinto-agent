"""Reply-agent responses wait for a person: nothing is sent and no meeting is booked until approval."""
from datetime import timedelta, timezone

import pytest

import agent.graph as ag
from agent import state as st
from agent.calendar_provider import parse_iso
from storage import db
from tests.helpers import DEMO, Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, review=True)
    yield e
    e.close()


def meetings(tid="acme.io"):
    return db.query("SELECT * FROM calendar_events WHERE kind='meeting' AND domain=?", (tid,))


def approve(env, s, tid="acme.io", **edits):
    d = s["pending_drafts"][-1]
    return ag.approve_reply(tid, d["id"], edits.get("subject"), edits.get("body"), graph=env.graph)


def test_reply_is_drafted_not_sent(env):
    tid = env.open_thread()
    s = env.reply(tid, "Sounds interesting, happy to chat.")
    assert s["reply_status"] == st.REPLY_DRAFT_READY and env.sender.sent == []
    d = s["pending_drafts"][0]
    assert d["kind"] == "propose_times" and len(d["slots"]) == 3 and d["subject"].startswith("Re: ")
    assert db.query_one("SELECT status FROM threads WHERE thread_id=?", (tid,))["status"] == st.REPLY_DRAFT_READY
    s = approve(env, s)
    assert s["reply_status"] == st.TIMES_PROPOSED and s["pending_drafts"] == [] and len(env.sender.sent) == 1
    assert env.sender.sent[0]["to"] == DEMO and "IST (Asia/Kolkata" in env.sender.sent[0]["text"]


def test_reviewer_edits_are_what_gets_sent(env):
    tid = env.open_thread()
    s = env.reply(tid, "How does evidence collection work?")
    assert s["pending_drafts"][0]["kind"] == "info"
    s = approve(env, s, subject="Re: your question", body="Hi Priya,\n\nEdited answer from a human.\n\nBest,\nSprinto")
    m = env.sender.sent[-1]
    assert m["subject"] == "Re: your question" and "Edited answer from a human." in m["text"]
    assert s["reply_status"] == st.INFO_SENT
    logged = [r["detail"] for r in db.query("SELECT detail FROM activity_log WHERE event='REPLY_DRAFT_APPROVED'")]
    assert any('"edited": true' in (x or "") for x in logged)


def test_booking_happens_only_on_approval(env):
    tid = env.open_thread()
    s = approve(env, env.reply(tid, "interested"))
    chosen = s["last_proposed"][1]
    s = env.reply(tid, "Option 2 works")
    assert s["reply_status"] == st.REPLY_DRAFT_READY
    d = s["pending_drafts"][0]
    assert d["kind"] == "booking_confirmation" and d["slot"] == chosen
    assert meetings() == [] and not s.get("event_id")                     # not booked yet
    n = len(env.sender.sent)
    s = approve(env, s)
    assert s["reply_status"] == st.MEETING_BOOKED and s["meeting_status"] == "CONFIRMED" and s["confirmed_slot"] == chosen
    assert len(meetings()) == 1 and len(env.sender.sent) == n + 1
    assert "BEGIN:VCALENDAR" in bytes(env.sender.sent[-1]["attachments"][0]["content"]).decode()


def test_slot_taken_before_approval_sends_nothing_and_drafts_alternatives(env):
    tid = env.open_thread()
    s = approve(env, env.reply(tid, "interested"))
    s = env.reply(tid, "Option 1 works")
    slot = s["pending_drafts"][0]["slot"]
    start = parse_iso(slot)
    db.execute("INSERT INTO calendar_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",          # someone grabs the slot
               ("busy-race", "busy", "Board call", start.astimezone(timezone.utc).isoformat(),
                (start + timedelta(minutes=30)).astimezone(timezone.utc).isoformat(), None, None, "busy", None, db.now_iso(), db.now_iso()))
    n = len(env.sender.sent)
    s = approve(env, s)
    assert len(env.sender.sent) == n and meetings() == []                    # nothing sent, nothing booked
    assert s["reply_status"] == st.REPLY_DRAFT_READY and slot in s["rejected_slots"]
    alt = s["pending_drafts"][0]
    assert alt["kind"] == "propose_times" and slot not in alt["slots"] and len(alt["slots"]) == 3
    assert "no longer available" in alt["body"] or alt["body"]               # drafted by the agent


def test_discard_sends_nothing_and_unsent_slots_dont_count(env):
    tid = env.open_thread()
    s = env.reply(tid, "interested")
    offered = s["pending_drafts"][0]["slots"]
    s = ag.discard_reply(tid, s["pending_drafts"][0]["id"], graph=env.graph)
    assert env.sender.sent == [] and s["reply_status"] == st.ESCALATED and s["pending_drafts"] == []
    assert not (set(offered) & set(s["times_proposed"])) and not (set(offered) & set(s.get("rejected_slots", [])))


def test_new_prospect_message_supersedes_stale_draft(env):
    tid = env.open_thread()
    s = env.reply(tid, "interested")
    old = s["pending_drafts"][0]
    s = env.reply(tid, "How does evidence collection work?")
    assert [d["kind"] for d in s["pending_drafts"]] == ["info"] and old["id"] not in [d["id"] for d in s["pending_drafts"]]
    assert not (set(old["slots"]) & set(s["times_proposed"]))
    assert db.query("SELECT 1 FROM activity_log WHERE event='REPLY_DRAFT_SUPERSEDED'")
    assert env.sender.sent == []


def test_no_reply_clock_paused_while_draft_waits(env):
    tid = env.open_thread()
    env.reply(tid, "interested")
    assert ag.check_deadlines(graph=env.graph, now=env.now + timedelta(hours=200)) == []


def test_unknown_or_already_handled_draft_is_ignored(env):
    tid = env.open_thread()
    s = env.reply(tid, "interested")
    did = s["pending_drafts"][0]["id"]
    approve(env, s)
    again = ag.approve_reply(tid, did, graph=env.graph)
    assert "not waiting for review" in again["skip_reason"] and len(env.sender.sent) == 1


def test_web_thread_review(env, monkeypatch):
    from fastapi.testclient import TestClient
    from config import settings as S
    monkeypatch.setattr(S, "PUBLIC_ACCESS", True)
    monkeypatch.setattr(ag, "get_graph", lambda: env.graph)
    from sprinto_web.app import app
    c = TestClient(app)
    tid = env.open_thread()
    s = env.reply(tid, "interested")
    page = c.get(f"/threads/{tid}").text
    assert "Agent reply waiting for your review" in page and "Send reply" in page
    assert tid in c.get("/drafts").text
    did = s["pending_drafts"][0]["id"]
    r = c.post(f"/threads/{tid}/draft/{did}", data={"action": "send", "subject": "Re: times", "body": s["pending_drafts"][0]["body"]},
               follow_redirects=False)
    assert "msg=Sent" in r.headers["location"] and len(env.sender.sent) == 1
    assert "Agent reply waiting for your review" not in c.get(f"/threads/{tid}").text
