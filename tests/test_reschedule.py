"""Rescheduling (rubric 3.9): updates the existing event; never repeats rejected slots; fresh availability check."""
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from agent import state as st
from agent.calendar_provider import parse_iso
from storage import db
from tests.helpers import Env

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


def book(env, tid="acme.io"):
    env.open_thread(tid)
    env.reply(tid, "interested")
    s = env.reply(tid, "Option 1 works")
    assert s["reply_status"] == st.MEETING_BOOKED
    return s


def meetings(tid):
    return db.query("SELECT * FROM calendar_events WHERE kind='meeting' AND domain=?", (tid,))


def free_slot_text(env, s):
    """An exact, free slot not yet proposed or rejected, as 'YYYY-MM-DD at HH:MM IST'."""
    exclude = set(s["times_proposed"]) | set(s["rejected_slots"])
    iso = env.cal.free_slots(env.now, 6, exclude)[-1]
    d = parse_iso(iso).astimezone(IST)
    return iso, f"{d.date().isoformat()} at {d.strftime('%H:%M')} IST"


def test_reschedule_without_time_keeps_event_and_proposes_new_slots(env):
    s = book(env)
    eid, old = s["event_id"], s["confirmed_slot"]
    s = env.reply("acme.io", "Something came up, can we reschedule?")
    assert s["meeting_status"] == "RESCHEDULE_REQUESTED" and s["reply_status"] == st.TIMES_PROPOSED
    assert s["reply_round"] == 0                       # fresh negotiation, not a counterproposal round
    assert old in s["rejected_slots"]
    new = s["times_proposed"][-3:]
    assert len(new) == 3 and old not in new and not (set(new) & set(s["rejected_slots"]))
    ev = db.query_one("SELECT * FROM calendar_events WHERE event_id=?", (eid,))
    assert ev["status"] == "confirmed" and parse_iso(ev["start_utc"]) == parse_iso(old)  # kept until a new slot is confirmed
    s = env.reply("acme.io", "Option 2 works")
    assert s["reply_status"] == st.MEETING_BOOKED and s["meeting_status"] == "RESCHEDULED"
    assert s["event_id"] == eid and s["confirmed_slot"] == new[1]
    rows = meetings("acme.io")
    assert len(rows) == 1 and parse_iso(rows[0]["start_utc"]) == parse_iso(new[1])  # same event, moved
    ics = bytes(env.sender.sent[-1]["attachments"][0]["content"]).decode()
    assert f"UID:{eid}@sprinto-demo" in ics and "SEQUENCE:1" in ics


def test_reschedule_to_exact_free_slot_updates_same_event(env):
    s = book(env)
    eid = s["event_id"]
    target, text = free_slot_text(env, s)
    s = env.reply("acme.io", f"Can we move it to {text}?")
    assert s["reply_status"] == st.MEETING_BOOKED and s["meeting_status"] == "RESCHEDULED"
    assert s["event_id"] == eid and s["confirmed_slot"] == target
    assert len(meetings("acme.io")) == 1


def test_second_reschedule_increments_ics_sequence(env):
    s = book(env)
    _, text = free_slot_text(env, s)
    s = env.reply("acme.io", f"Can we move it to {text}?")
    _, text2 = free_slot_text(env, s)
    s = env.reply("acme.io", f"Sorry, can we reschedule again to {text2}?")
    assert s["event_sequence"] == 2
    assert "SEQUENCE:2" in bytes(env.sender.sent[-1]["attachments"][0]["content"]).decode()
    assert len(meetings("acme.io")) == 1


def test_reschedule_to_busy_slot_keeps_event_and_offers_alternatives(env):
    s = book(env)
    eid, old = s["event_id"], s["confirmed_slot"]
    day = env.now.astimezone(IST).date() + timedelta(days=1)       # seeded stand-up 09:30-10:00 IST every weekday
    while day.weekday() >= 5:
        day += timedelta(days=1)
    s = env.reply("acme.io", f"Can we move it to {day.isoformat()} at 09:30 IST?")
    assert s["reply_status"] == st.TIMES_PROPOSED and s["meeting_status"] == "RESCHEDULE_REQUESTED"
    assert s["event_id"] == eid and s["confirmed_slot"] == old
    assert parse_iso(meetings("acme.io")[0]["start_utc"]) == parse_iso(old)


def test_reschedule_without_booking_is_a_counterproposal(env):
    env.open_thread()
    env.reply("acme.io", "interested")
    s = env.reply("acme.io", "Can we reschedule to later?")
    assert s["reply_round"] == 1 and s["reply_status"] == st.TIMES_PROPOSED
    assert not s.get("event_id")


def test_option_number_refers_to_latest_batch_even_if_shorter(env, monkeypatch):
    """If the latest offer has fewer than 3 slots, 'option 1' must still mean the first slot of THAT offer."""
    from agent import tools as T
    s = book(env)
    real = T.check_calendar

    def two_slots(state, now, window=None, count=3, sleep=None):
        r = real(state, now, window, count)
        r["slots"] = r["slots"][:2]
        return r
    monkeypatch.setattr(T, "check_calendar", two_slots)
    s = env.reply("acme.io", "Can we reschedule?")
    assert len(s["last_proposed"]) == 2
    first = s["last_proposed"][0]
    s = env.reply("acme.io", "Option 1 works")
    assert s["confirmed_slot"] == first and s["meeting_status"] == "RESCHEDULED"
