"""Rubric 3.11: 'Send/booking timeout -> reconcile provider state before retrying'; unresolved -> SEND_UNCERTAIN."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import pipeline
from config import settings as S
from outreach import send as sm
from storage import db
from tests.helpers import DEMO, Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    sm.ResendSender._lookup_blocked = None
    yield e
    sm.ResendSender._lookup_blocked = None
    e.close()


class Provider:
    """Fake email provider. `land` = whether a timed-out send actually went out; `lookup` = found|blocked|error."""
    name = "resend"

    def __init__(self, timeouts=1, land=True, lookup="found"):
        self.timeouts, self.land, self.lookup = timeouts, land, lookup
        self.delivered, self.calls, self.lookups = [], [], 0

    def send(self, to, subject, text, attachments, key):
        self.calls.append(key)
        if any(d["key"] == key for d in self.delivered):          # provider-side idempotency
            return next(d["id"] for d in self.delivered if d["key"] == key)
        if self.timeouts > 0:
            self.timeouts -= 1
            if self.land:
                self.delivered.append({"key": key, "id": f"re_{len(self.delivered)}", "subject": subject, "text": text})
            raise sm.SendTimeout("read timed out")
        self.delivered.append({"key": key, "id": f"re_{len(self.delivered)}", "subject": subject, "text": text})
        return self.delivered[-1]["id"]

    def find_sent(self, to, subject, text, since):
        self.lookups += 1
        if self.lookup == "blocked":
            raise sm.LookupUnavailable("send-only key")
        if self.lookup == "error":
            raise RuntimeError("500 from provider")
        for d in self.delivered:
            if d["subject"] == subject and d["text"] == text:
                return d["id"]
        return None


def op_row(op):
    return db.query_one("SELECT * FROM operations WHERE op_id=?", (op,))


def test_timeout_then_found_at_provider_no_second_send(env):
    p = Provider(timeouts=1, land=True, lookup="found")
    r = sm.send_once("op1", "initial_email", "acme.io", "S", "Body", p)
    assert r["status"] == sm.SENT and r["provider_id"] == "re_0"
    assert p.calls == ["op1"] and len(p.delivered) == 1               # reconciled instead of retrying
    assert "after timeout #1: FOUND via provider lookup" in r["reconcile"]


def test_timeout_not_landed_then_retry_same_key(env):
    p = Provider(timeouts=1, land=False, lookup="found")
    r = sm.send_once("op2", "initial_email", "acme.io", "S", "Body", p)
    assert r["status"] == sm.SENT and p.calls == ["op2", "op2"] and len(p.delivered) == 1
    assert "after timeout #1: NOT_FOUND via provider lookup" in r["reconcile"]


def test_send_only_key_falls_back_to_idempotency_key(env):
    p = Provider(timeouts=1, land=True, lookup="blocked")
    r = sm.send_once("op3", "initial_email", "acme.io", "S", "Body", p)
    assert r["status"] == sm.SENT and r["provider_id"] == "re_0"
    assert p.calls == ["op3", "op3"] and len(p.delivered) == 1           # provider deduped the retry
    assert "relying on idempotency key" in r["reconcile"][0]


def test_lookup_error_is_unknown_and_retry_uses_same_key(env):
    """A failing lookup is UNKNOWN (never 'not sent'): the retry reuses the idempotency key, so no duplicate."""
    p = Provider(timeouts=1, land=True, lookup="error")
    r = sm.send_once("op4", "initial_email", "acme.io", "S", "Body", p)
    assert r["status"] == sm.SENT and r["provider_id"] == "re_0"
    assert p.calls == ["op4", "op4"] and len(p.delivered) == 1
    assert "UNKNOWN via lookup failed" in r["reconcile"][0]


def test_all_attempts_time_out_and_unverifiable_is_uncertain(env):
    class Dead(Provider):
        def send(self, *a, **k):
            self.calls.append(a[-1])
            raise sm.SendTimeout("read timed out")
    p = Dead(lookup="blocked")
    r = sm.send_once("op4b", "initial_email", "acme.io", "S", "Body", p)
    assert r["status"] == sm.UNCERTAIN and len(p.calls) == S.MAX_FETCH_ATTEMPTS
    assert op_row("op4b")["status"] == "SEND_UNCERTAIN" and json.loads(op_row("op4b")["detail"])["reconcile"]


def test_crash_mid_send_reconciles_before_sending_again(env):
    p = Provider(timeouts=0, lookup="found")
    p.delivered.append({"key": "op5", "id": "re_9", "subject": "S", "text": "Body"})   # it went out...
    payload = json.dumps({"sender": "resend", "to": DEMO, "subject": "S", "text": "Body"})
    db.execute("INSERT INTO operations (op_id, kind, domain, status, attempts, created_at, updated_at, payload)"
               " VALUES (?,?,?,?,?,?,?,?)",                                          # ...but we crashed
               ("op5", "initial_email", "acme.io", "PENDING", 1, db.now_iso(), db.now_iso(), payload))
    r = sm.send_once("op5", "initial_email", "acme.io", "S", "Body", p)
    assert r["status"] == sm.SENT and r["provider_id"] == "re_9" and p.calls == []


def test_uncertain_is_stored_with_payload_and_sweep_finds_it(env):
    p = Provider(timeouts=5, land=False, lookup="blocked")
    r = sm.send_once("op6", "reply_info", "acme.io", "S", "Body", p)
    assert r["status"] == sm.UNCERTAIN
    payload = json.loads(op_row("op6")["payload"])
    assert payload == {"sender": "resend", "to": DEMO, "subject": "S", "text": "Body", "has_attachments": False}
    # later the provider turns out to have it (e.g. a full-access key, or it arrived late)
    p.lookup = "found"
    p.delivered.append({"key": "op6", "id": "re_late", "subject": "S", "text": "Body"})
    calls_before = list(p.calls)
    found = []
    out = sm.reconcile_uncertain(lambda name: p, lambda row, pid: found.append((row["op_id"], pid)))
    assert out[0]["state"] == sm.FOUND and op_row("op6")["status"] == sm.SENT and op_row("op6")["provider_id"] == "re_late"
    assert found == [("op6", "re_late")] and p.calls == calls_before        # sweep never re-sends
    assert db.query("SELECT 1 FROM activity_log WHERE event='SEND_RECONCILED'")


def test_sweep_leaves_not_found_uncertain_for_a_human(env):
    p = Provider(timeouts=5, land=False, lookup="found")
    sm.send_once("op7", "initial_email", "acme.io", "S", "Body", p)
    sent = len(p.calls)
    out = sm.reconcile_uncertain(lambda name: p)
    assert out[0]["state"] == sm.NOT_FOUND and op_row("op7")["status"] == sm.UNCERTAIN and len(p.calls) == sent


def test_reconciled_initial_email_marks_company_sent_and_opens_thread(env):
    log = {"domain": "acme.io", "status": "SEND_UNCERTAIN", "escalation_reason": "timeout",
           "facts": [{"field": "company_name", "verified": True, "value": "Acme"}],
           "contact": {"selected": {"name": "Priya Testwal", "first_name": "Priya", "title": "CISO"}}}
    pipeline.save_log("acme.io", log)
    row = {"op_id": "op8", "kind": "initial_email", "domain": "acme.io",
           "payload": {"sender": "resend", "subject": "SOC 2 at Acme", "text": "DEMO - hi"}}
    pipeline.finalise_reconciled_send(row, "re_8")
    c = db.query_one("SELECT status, email_status FROM companies WHERE domain='acme.io'")
    assert c == {"status": "EMAIL_SENT", "email_status": "SENT"}
    t = db.query_one("SELECT * FROM threads WHERE thread_id='acme.io'")
    assert t and t["status"] == "AWAITING_REPLY"
    assert db.query_one("SELECT provider_id FROM messages WHERE message_id='out-op8'")["provider_id"] == "re_8"


# ---------------- the real ResendSender lookup, against a fake resend module
class FakeResendModule:
    class exceptions:
        class ResendError(Exception):
            pass

    def __init__(self, emails=None, restricted=False):
        self.emails, self.restricted, self.list_calls = emails or [], restricted, 0
        outer = self

        class Emails:
            @staticmethod
            def list(params):
                outer.list_calls += 1
                if outer.restricted:
                    raise outer.exceptions.ResendError("This API key is restricted to only send emails")
                return {"data": [{k: v for k, v in e.items() if k != "text"} for e in outer.emails], "has_more": False}

            @staticmethod
            def get(eid):
                return next(e for e in outer.emails if e["id"] == eid)
        self.Emails = Emails


def resend_sender(monkeypatch, fake):
    monkeypatch.setattr(S, "RESEND_API_KEY", "re_test")
    s = sm.ResendSender()
    s.resend = fake
    return s


def test_resend_lookup_matches_exact_text_recipient_and_time(env, monkeypatch):
    now = datetime.now(timezone.utc)
    ts = lambda d: (now + d).isoformat()
    fake = FakeResendModule([
        {"id": "e3", "to": [DEMO], "subject": "S", "text": "Body v2", "created_at": ts(timedelta(seconds=5))},   # wrong text
        {"id": "e2", "to": [DEMO], "subject": "S", "text": "Body", "created_at": ts(timedelta(seconds=3))},      # match
        {"id": "e1", "to": [DEMO], "subject": "S", "text": "Body", "created_at": ts(-timedelta(hours=2))},       # too old
    ])
    s = resend_sender(monkeypatch, fake)
    assert s.find_sent(DEMO, "S", "Body", now) == "e2"
    assert s.find_sent(DEMO, "Other", "Body", now) is None
    assert s.find_sent(DEMO, "S", "Body", now + timedelta(hours=1)) is None


def test_send_only_resend_key_detected_once(env, monkeypatch):
    fake = FakeResendModule(restricted=True)
    s = resend_sender(monkeypatch, fake)
    with pytest.raises(sm.LookupUnavailable):
        s.find_sent(DEMO, "S", "Body", datetime.now(timezone.utc))
    with pytest.raises(sm.LookupUnavailable):
        s.find_sent(DEMO, "S", "Body", datetime.now(timezone.utc))
    assert fake.list_calls == 1                                               # cached, no repeated API calls
