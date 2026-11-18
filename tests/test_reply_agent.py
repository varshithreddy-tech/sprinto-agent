from datetime import timedelta

import pytest

from agent import state as st
from agent.calendar_provider import is_working_slot, parse_iso
from storage import db
from tests.helpers import DEMO, Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


def activity(tid):
    return [r["event"] for r in db.query("SELECT event FROM activity_log WHERE domain=? ORDER BY id", (tid,))]


def test_interested_proposes_three_checked_slots(env):
    tid = env.open_thread()
    s = env.reply(tid, "Sounds interesting, happy to chat.")
    assert s["reply_status"] == st.TIMES_PROPOSED
    assert s["calendar_checked"] is True and s["checked_at"]
    assert len(s["times_proposed"]) == 3
    for iso in s["times_proposed"]:
        start = parse_iso(iso)
        assert is_working_slot(start)
        assert env.cal.is_free(start), f"{iso} overlaps a busy block"
        assert start > env.now
        assert start < env.now + timedelta(days=30)
    assert len(env.sender.sent) == 1
    mail = env.sender.sent[0]
    assert mail["to"] == DEMO
    assert "IST (Asia/Kolkata, UTC+05:30)" in mail["text"]
    assert "TIMES_PROPOSED" in activity(tid)


def test_confirm_slot_books_and_sends_ics(env):
    tid = env.open_thread()
    s = env.reply(tid, "I'm interested.")
    chosen = s["times_proposed"][1]
    s = env.reply(tid, "Option 2 works for me, send the invite.")
    assert s["reply_status"] == st.MEETING_BOOKED
    assert s["meeting_status"] == "CONFIRMED"
    assert s["confirmed_slot"] == chosen and s["event_id"]
    ev = db.query_one("SELECT * FROM calendar_events WHERE event_id=?", (s["event_id"],))
    assert ev and ev["kind"] == "meeting" and ev["attendee"] == DEMO
    last = env.sender.sent[-1]
    assert last["attachments"] and last["attachments"][0]["filename"] == "invite.ics"
    ics = bytes(last["attachments"][0]["content"]).decode()
    assert "BEGIN:VCALENDAR" in ics and f"UID:{s['event_id']}@sprinto-demo" in ics and f"mailto:{DEMO}" in ics
    # the two un-chosen proposed slots are now tracked as rejected
    assert set(s["times_proposed"]) - {chosen} <= set(s["rejected_slots"])


def test_confirm_busy_slot_proposes_alternatives(env):
    tid = env.open_thread()
    env.reply(tid, "interested")
    # stand-up is seeded daily 09:30-10:00 IST -> pick Thursday 19 Nov 09:30 IST explicitly
    s = env.reply(tid, "Can we do 2026-11-19 at 09:30 IST?")
    assert s["reply_status"] == st.TIMES_PROPOSED
    assert "2026-11-19T09:30:00+05:30" in s["rejected_slots"]
    assert not s.get("event_id")


def test_slot_without_timezone_asks_for_clarification(env):
    tid = env.open_thread()
    env.reply(tid, "interested")
    s = env.reply(tid, "How about 2026-11-24 at 16:00?")
    assert s["reply_status"] == st.CLARIFICATION_REQUESTED
    assert s["prospect_tz"] is None  # never inferred from organizer tz
    assert not s.get("event_id")


def test_counterproposes_twice_escalates(env):
    tid = env.open_thread()
    s1 = env.reply(tid, "interested")
    first = set(s1["times_proposed"])
    s2 = env.reply(tid, "None of these work, next week afternoon?")
    assert s2["reply_round"] == 1 and s2["reply_status"] == st.TIMES_PROPOSED
    assert first <= set(s2["rejected_slots"])
    assert not (set(s2["times_proposed"][3:]) & first)  # never repeat rejected slots
    s3 = env.reply(tid, "None of these work either, next week?")
    assert s3["reply_round"] == 2
    assert s3["reply_status"] == st.ESCALATED
    assert "counterproposal" in s3["escalation_reason"]


def test_pricing_hands_off(env):
    tid = env.open_thread()
    s = env.reply(tid, "What does Sprinto cost? Send pricing.")
    assert s["reply_status"] == st.ESCALATED
    assert "Pricing" in s["escalation_reason"]
    assert env.sender.sent == []


def test_opt_out_suppressed(env):
    tid = env.open_thread()
    s = env.reply(tid, "Please unsubscribe me and stop emailing.")
    assert s["suppressed"] is True and s["reply_status"] == st.OPTED_OUT
    s = env.reply(tid, "Actually I'm interested now")
    assert "suppressed" in s["skip_reason"]
    assert env.sender.sent == []


def test_out_of_office_classified_separately(env):
    tid = env.open_thread()
    s = env.reply(tid, "Automatic reply: I am out of office until Monday.")
    assert s["reply_status"] == st.OUT_OF_OFFICE
    assert s["classification"]["category"] == "out_of_office"
    assert s["meeting_status"] == "NONE" and not s.get("escalation_reason")
    assert env.sender.sent == []
    assert "OUT_OF_OFFICE" in activity(tid)


def test_info_request_answers_without_assuming_meeting(env):
    tid = env.open_thread()
    s = env.reply(tid, "How does evidence collection work?")
    assert s["reply_status"] == st.INFO_SENT and s["meeting_status"] == "NONE"
    assert s["times_proposed"] == []


def test_reject_and_referral(env):
    tid = env.open_thread()
    assert env.reply(tid, "Not interested, thanks.")["reply_status"] == st.REJECTED
    tid2 = env.open_thread("beta.io")
    s = env.reply(tid2, "You should talk to Asha, our Head of IT.")
    assert s["reply_status"] == st.ESCALATED and s["referral"]["name"] == "Asha Rao"


def test_agent_messages_and_duplicates_ignored(env):
    tid = env.open_thread()
    s = env.reply(tid, "interested", mid="m-1")
    n = len(env.sender.sent)
    s = env.reply(tid, "interested", mid="m-1")
    assert "already processed" in s["skip_reason"] and len(env.sender.sent) == n
    agent_mid = s["agent_message_ids"][-1]
    s = env.graph.invoke({"incoming": {"message_id": agent_mid, "body": "Hi Priya", "author": "agent"}},
                         {"configurable": {"thread_id": tid}})
    assert "loop prevention" in s["skip_reason"] and len(env.sender.sent) == n


def test_no_reply_deadline_escalates(env):
    from agent.graph import check_deadlines
    tid = env.open_thread()
    assert check_deadlines(graph=env.graph, now=env.now) == []           # before deadline
    env.now = env.now + timedelta(hours=49)
    env.graph = env.new_graph()
    res = check_deadlines(graph=env.graph, now=env.now)
    assert res and res[0]["status"] == st.ESCALATED and "No reply" in res[0]["reason"]
