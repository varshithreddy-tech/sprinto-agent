"""Rubric 3.11 'Logging fails -> preserve successful actions; retry logging only'."""
import json

import pytest

import agent.graph as ag
import pipeline
from agent import state as st
from config import settings as S
from storage import db
from tests.helpers import Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "ACTIVITY_SPOOL_PATH", tmp_path / "spool.jsonl")
    yield e
    e.close()


class LogOutage:
    """Makes every activity-log write fail until .end() is called."""
    def __init__(self, monkeypatch):
        self.real = db.log_entries
        self.down = True
        monkeypatch.setattr(db, "log_entries", self)

    def __call__(self, *a, **k):
        if self.down:
            raise RuntimeError("database is locked")
        return self.real(*a, **k)

    def end(self):
        self.down = False


def events(domain):
    return [r["event"] for r in db.query("SELECT event FROM activity_log WHERE domain=? ORDER BY ts, id", (domain,))]


def test_failed_entries_survive_the_next_message(env, monkeypatch):
    """Regression: entries that failed to write used to be wiped by the next message."""
    tid = env.open_thread()
    out = LogOutage(monkeypatch)
    s = env.reply(tid, "interested")
    assert s["crm_logged"] is False and len(env.sender.sent) == 1          # send preserved
    kept = [e["event"] for e in s["pending_log"]]
    assert {"REPLY_CLASSIFIED", "TIMES_PROPOSED", "REPLY_SENT"} <= set(kept)
    out.end()
    s = env.reply(tid, "Option 1 works")
    assert s["crm_logged"] is True and s["pending_log"] == []
    ev = events(tid)
    assert "TIMES_PROPOSED" in ev and "MEETING_CONFIRMED" in ev            # old AND new entries written
    assert ev.index("TIMES_PROPOSED") < ev.index("MEETING_CONFIRMED")      # original order/time kept


def test_sweep_retries_without_a_new_message_and_only_logging(env, monkeypatch):
    tid = env.open_thread()
    out = LogOutage(monkeypatch)
    s = env.reply(tid, "interested")
    proposed, sent = s["times_proposed"], len(env.sender.sent)
    stamps = {e["entry_id"]: e["ts"] for e in s["pending_log"]}
    assert ag.threads_with_unlogged_entries(env.graph) == [tid]
    r = ag.retry_failed_logging(graph=env.graph)                           # DB still down
    assert r["threads_retried"] == [] and r["still_pending"] >= 3
    out.end()
    r = ag.retry_failed_logging(graph=env.graph)
    assert r["threads_retried"] == [tid] and r["still_pending"] == 0
    s = ag.get_state(tid, graph=env.graph)
    assert s["crm_logged"] is True and s["times_proposed"] == proposed     # nothing re-run
    assert len(env.sender.sent) == sent                                    # no duplicate email
    rows = db.query("SELECT entry_id, ts FROM activity_log WHERE domain=?", (tid,))
    written = {r["entry_id"]: r["ts"] for r in rows}
    assert all(written[i] == ts for i, ts in stamps.items())               # original timestamps
    assert ag.retry_failed_logging(graph=env.graph)["threads_retried"] == []  # idempotent
    assert len(db.query("SELECT 1 FROM activity_log WHERE domain=?", (tid,))) == len(rows)


def test_log_entries_is_all_or_nothing_and_exactly_once(env):
    e1, e2 = db.new_entry("A"), db.new_entry("B")
    db.log_entries("t.io", [e1])
    db.log_entries("t.io", [e1, e2])                                       # e1 again: ignored
    assert events("t.io") == ["A", "B"]

    class Boom(dict):
        def get(self, k, d=None):
            raise RuntimeError("serialisation failed")
    with pytest.raises(RuntimeError):
        db.log_entries("u.io", [db.new_entry("C"), Boom(event="D")])
    assert events("u.io") == []                                            # nothing half-written


def test_pipeline_log_failure_never_fails_a_sent_domain(env, monkeypatch):
    out = LogOutage(monkeypatch)
    ok = db.log_activity_safe("acme.io", "EMAIL_SENT", {"op_id": "x"})
    assert ok is False and db.spool_count() == 1
    line = json.loads(open(S.ACTIVITY_SPOOL_PATH, encoding="utf-8").read().strip())
    assert line["domain"] == "acme.io" and line["event"] == "EMAIL_SENT" and line["entry_id"]
    r = db.flush_spool()
    assert r == {"written": 0, "remaining": 1}                             # still down: kept
    out.end()
    assert db.flush_spool() == {"written": 1, "remaining": 0}
    assert db.spool_count() == 0 and events("acme.io") == ["EMAIL_SENT"]
    assert db.flush_spool() == {"written": 0, "remaining": 0}


def test_batch_status_survives_logging_outage(env, monkeypatch):
    out = LogOutage(monkeypatch)
    monkeypatch.setattr(pipeline, "research_company", lambda d, progress=None: {
        "domain": d, "status": "LOW_PRIORITY", "facts": [], "qualification": {"total": 40}, "rounds": [], "pages": []})
    res = pipeline.run_batch(["x.io"])
    assert res[0]["status"] == "LOW_PRIORITY"                              # not ERROR
    assert db.query_one("SELECT status FROM companies WHERE domain='x.io'")["status"] == "LOW_PRIORITY"
    assert db.spool_count() >= 1
    out.end()
    ag.retry_failed_logging(graph=env.graph)
    assert "QUALIFICATION" in events("x.io") and db.spool_count() == 0


def test_watcher_retries_logging(env, monkeypatch):
    calls = []
    monkeypatch.setattr(ag, "retry_failed_logging", lambda *a, **k: calls.append("log") or {})
    monkeypatch.setattr(ag, "check_deadlines", lambda *a, **k: calls.append("deadline") or [])
    t = ag.start_deadline_watcher(interval_minutes=0.0005)
    import time
    end = time.time() + 3
    while calls.count("log") < 2 and time.time() < end:
        time.sleep(0.02)
    t.stop_event.set()
    t.join(timeout=2)
    assert calls.count("log") >= 2 and calls.index("log") < calls.index("deadline")
