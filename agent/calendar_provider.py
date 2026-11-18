"""Calendar behind an interface so Google Calendar can be plugged in later.

DemoCalendar stores busy blocks + booked meetings in SQLite. Organizer timezone Asia/Kolkata;
weekdays 09:00–18:00; 30-minute meetings; 30-day horizon. It never claims access to the prospect's calendar.
"""
from __future__ import annotations

import random
import uuid
from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from config import settings as S
from storage import db


class CalendarError(Exception):
    pass


def org_tz() -> ZoneInfo:
    return ZoneInfo(S.ORGANIZER_TZ)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(org_tz()).replace(microsecond=0).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def is_working_slot(start: datetime) -> bool:
    local = start.astimezone(org_tz())
    end = local + timedelta(minutes=S.MEETING_MINUTES)
    return (local.weekday() < 5 and time(S.WORKDAY_START_H) <= local.time()
            and (end.time() <= time(S.WORKDAY_END_H) and end.date() == local.date()))


class CalendarProvider(ABC):
    """Interface. Implementations: DemoCalendar (SQLite). A GoogleCalendarProvider can implement the same methods."""

    @abstractmethod
    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]: ...

    @abstractmethod
    def create_event(self, start: datetime, title: str, attendee: str, domain: str, op_id: str) -> str: ...

    @abstractmethod
    def update_event(self, event_id: str, start: datetime) -> str: ...

    @abstractmethod
    def get_event(self, event_id: str) -> dict | None: ...

    # ---- shared logic ---------------------------------------------------------
    def is_free(self, start: datetime, ignore_event: str | None = None) -> bool:
        end = start + timedelta(minutes=S.MEETING_MINUTES)
        return not any(b_s < end and start < b_e and eid != ignore_event for b_s, b_e, eid in self.busy(start, end))

    def free_slots(self, now: datetime, count: int, exclude: set[str], window: dict | None = None,
                   spread_days: bool = True) -> list[str]:
        """Calendar-checked free working slots in the horizon, never in `exclude` (rejected/already proposed)."""
        tz = org_tz()
        start_day = now.astimezone(tz).date()
        horizon = now + timedelta(days=S.SCHEDULING_HORIZON_DAYS)
        earliest = now + timedelta(hours=2)
        w_from = date.fromisoformat(window["date_from"]) if window and window.get("date_from") else None
        w_to = date.fromisoformat(window["date_to"]) if window and window.get("date_to") else None
        w_t0 = time.fromisoformat(window["earliest_time"]) if window and window.get("earliest_time") else None
        w_t1 = time.fromisoformat(window["latest_time"]) if window and window.get("latest_time") else None
        w_tz = ZoneInfo(window["timezone"]) if window and window.get("timezone") else tz
        busy = self.busy(now, horizon)
        out: list[str] = []
        preferred = [(11, 0), (15, 0), (10, 30), (16, 0), (12, 0), (14, 30), (9, 30), (17, 0)]
        d = start_day
        while d <= horizon.astimezone(tz).date() and len(out) < count:
            if d.weekday() < 5:
                day_slots = []
                # preferred times first, then every half hour
                order = preferred[(d.toordinal() % 3):] + preferred[:(d.toordinal() % 3)]
                order += [(h, m) for h in range(S.WORKDAY_START_H, S.WORKDAY_END_H) for m in (0, 30)]
                for h, m in dict.fromkeys(order):
                    s = datetime.combine(d, time(h, m), tz)
                    if s < earliest or s > horizon or not is_working_slot(s):
                        continue
                    sw = s.astimezone(w_tz)
                    if w_from and sw.date() < w_from or w_to and sw.date() > w_to:
                        continue
                    if w_t0 and sw.time() < w_t0 or w_t1 and (sw + timedelta(minutes=S.MEETING_MINUTES)).time() > w_t1:
                        continue
                    iso = to_iso(s)
                    if iso in exclude:
                        continue
                    e = s + timedelta(minutes=S.MEETING_MINUTES)
                    if any(b_s < e and s < b_e for b_s, b_e, _ in busy):
                        continue
                    day_slots.append(iso)
                    if spread_days:
                        break
                out.extend(day_slots[: count - len(out)])
            d += timedelta(days=1)
        return out


class DemoCalendar(CalendarProvider):
    def __init__(self, conn=None):
        self.conn = conn

    def _q(self, sql, params=()):
        return db.query(sql, params, self.conn)

    def _x(self, sql, params=()):
        return db.execute(sql, params, self.conn)

    def busy(self, start, end):
        rows = self._q("SELECT event_id, start_utc, end_utc FROM calendar_events WHERE status!='cancelled' "
                       "AND start_utc < ? AND end_utc > ?", (end.astimezone(timezone.utc).isoformat(),
                                                             start.astimezone(timezone.utc).isoformat()))
        return [(parse_iso(r["start_utc"]), parse_iso(r["end_utc"]), r["event_id"]) for r in rows]

    def create_event(self, start, title, attendee, domain, op_id):
        existing = self._q("SELECT event_id FROM calendar_events WHERE op_id=?", (op_id,))
        if existing:
            return existing[0]["event_id"]  # idempotent booking
        eid = f"evt-{uuid.uuid4().hex[:12]}"
        end = start + timedelta(minutes=S.MEETING_MINUTES)
        self._x("INSERT INTO calendar_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (eid, "meeting", title, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(),
                 attendee, domain, "confirmed", op_id, db.now_iso(), db.now_iso()))
        return eid

    def update_event(self, event_id, start):
        end = start + timedelta(minutes=S.MEETING_MINUTES)
        cur = self._x("UPDATE calendar_events SET start_utc=?, end_utc=?, status='confirmed', updated_at=? WHERE event_id=?",
                      (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(), db.now_iso(), event_id))
        if cur.rowcount == 0:
            raise CalendarError(f"event {event_id} not found")
        return event_id

    def get_event(self, event_id):
        rows = self._q("SELECT * FROM calendar_events WHERE event_id=?", (event_id,))
        return rows[0] if rows else None

    def seed(self, today: date | None = None, force: bool = False) -> int:
        """A few busy blocks over the next 10 working days (deterministic per date)."""
        if not force and self._q("SELECT 1 FROM calendar_events WHERE kind='busy' LIMIT 1"):
            return 0
        tz = org_tz()
        d = today or datetime.now(tz).date()
        added, days = 0, 0
        titles = ["Team stand-up", "Pipeline review", "Customer call", "Focus block", "1:1", "Partner sync", "Lunch"]
        while days < 10:
            if d.weekday() < 5:
                rnd = random.Random(d.toordinal())
                blocks = {(9, 30, 30, "Team stand-up"), (13, 0, 60, "Lunch")}
                for _ in range(rnd.randint(1, 3)):
                    h = rnd.choice([10, 11, 12, 14, 15, 16, 17])
                    m = rnd.choice([0, 30])
                    dur = rnd.choice([30, 60, 90]) if h < 17 else 30
                    blocks.add((h, m, dur, rnd.choice(titles[1:6])))
                for h, m, dur, title in blocks:
                    s = datetime.combine(d, time(h, m), tz)
                    e = s + timedelta(minutes=dur)
                    self._x("INSERT INTO calendar_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (f"busy-{uuid.uuid4().hex[:10]}", "busy", title, s.astimezone(timezone.utc).isoformat(),
                             e.astimezone(timezone.utc).isoformat(), None, None, "busy", None, db.now_iso(), db.now_iso()))
                    added += 1
                days += 1
            d += timedelta(days=1)
        return added


def fmt_slot(iso: str, prospect_tz: str | None = None) -> str:
    """Exact date, time and timezone. Adds the prospect's timezone only if they stated one."""
    s = parse_iso(iso).astimezone(org_tz())
    e = s + timedelta(minutes=S.MEETING_MINUTES)
    txt = f"{s.strftime('%a %d %b %Y')}, {s.strftime('%H:%M')}–{e.strftime('%H:%M')} IST (Asia/Kolkata, UTC+05:30)"
    if prospect_tz and prospect_tz != S.ORGANIZER_TZ:
        ps = s.astimezone(ZoneInfo(prospect_tz))
        txt += f" = {ps.strftime('%a %d %b, %H:%M')} {prospect_tz}"
    return txt


_cal: CalendarProvider | None = None


def get_calendar() -> CalendarProvider:
    global _cal
    if _cal is None:
        _cal = DemoCalendar()
        _cal.seed()
    return _cal


def set_calendar(c: CalendarProvider | None) -> None:
    global _cal
    _cal = c
