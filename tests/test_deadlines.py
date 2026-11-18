"""No-reply deadline automation (rubric 3.8: wait until deadline, then hand off; pause only that prospect)."""
import time
from datetime import timedelta

import pytest

import agent.graph as ag
import pipeline
from agent import state as st
from storage import db
from tests.helpers import Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


def history(env, tid):
    return len(list(env.graph.get_state_history({"configurable": {"thread_id": tid}})))


def check(env, hours):
    return ag.check_deadlines(graph=env.graph, now=env.now + timedelta(hours=hours))


def escalations(tid):
    return [r for r in db.query("SELECT event FROM activity_log WHERE domain=? AND event='ESCALATED'", (tid,))]


def test_not_due_threads_are_not_invoked(env):
    tid = env.open_thread()
    before = history(env, tid)
    assert check(env, 47) == []
    assert history(env, tid) == before          # routine checks write no checkpoints


def test_due_thread_escalates_once(env):
    tid = env.open_thread()
    res = check(env, 49)
    assert res == [{"thread": tid, "status": st.ESCALATED, "reason": "No reply within 48h deadline"}]
    assert check(env, 50) == []                 # idempotent: escalated threads are no longer waiting
    assert len(escalations(tid)) == 1
    assert ag.LAST_DEADLINE_CHECK["at"] is not None


def test_only_the_due_prospect_is_paused(env):
    a = env.open_thread("a.io")
    b = env.open_thread("b.io")
    env.now = env.now + timedelta(hours=24)
    env.graph = env.new_graph()
    env.reply(b, "Sounds interesting, happy to chat.")   # b's deadline resets to now + 48h
    res = check(env, 25)                                # a: 49h since send; b: 25h since reply
    assert [r["thread"] for r in res] == [a]
    assert ag.get_state(b, graph=env.graph)["reply_status"] == st.TIMES_PROPOSED


def test_deadline_resets_after_each_agent_reply(env):
    tid = env.open_thread()
    env.now = env.now + timedelta(hours=40)
    env.graph = env.new_graph()
    s = env.reply(tid, "interested")
    assert s["reply_status"] == st.TIMES_PROPOSED
    assert check(env, 47) == []                 # 87h after first email, 47h after the proposal
    row = db.query_one("SELECT reply_deadline FROM threads WHERE thread_id=?", (tid,))
    assert row["reply_deadline"] == s["reply_deadline"]  # mirrored for the dashboard
    assert check(env, 49)[0]["status"] == st.ESCALATED


def test_booked_suppressed_and_stopped_threads_are_skipped(env):
    booked = env.open_thread("booked.io")
    env.reply(booked, "interested")
    env.reply(booked, "Option 1 works")
    optout = env.open_thread("optout.io")
    env.reply(optout, "Please unsubscribe me.")
    rejected = env.open_thread("rejected.io")
    env.reply(rejected, "Not interested, thanks.")
    assert check(env, 200) == []


def test_one_thread_failure_does_not_stop_others(env, monkeypatch):
    a = env.open_thread("a.io")
    b = env.open_thread("b.io")
    real = env.graph.invoke

    def flaky(inp, cfg, *args, **kw):
        if cfg["configurable"]["thread_id"] == a:
            raise RuntimeError("checkpoint locked")
        return real(inp, cfg, *args, **kw)
    monkeypatch.setattr(env.graph, "invoke", flaky)
    res = check(env, 49)
    assert [r["thread"] for r in res] == [b]
    assert "a.io" in ag.LAST_DEADLINE_CHECK["error"]
    assert db.query("SELECT 1 FROM activity_log WHERE domain='a.io' AND event='DEADLINE_CHECK_FAILED'")


def test_watcher_thread_runs_checks_repeatedly(monkeypatch):
    calls = []
    monkeypatch.setattr(ag, "check_deadlines", lambda *a, **k: calls.append(1) or [])
    t = ag.start_deadline_watcher(interval_minutes=0.0005)   # ~30 ms
    deadline = time.time() + 3
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert t.is_alive() and t.daemon and len(calls) >= 3
    t.stop_event.set()
    t.join(timeout=2)
    assert not t.is_alive()


def test_batch_run_sweeps_deadlines(env, monkeypatch):
    calls = []
    monkeypatch.setattr(ag, "check_deadlines", lambda *a, **k: calls.append(1) or [])
    monkeypatch.setattr(pipeline, "research_company", lambda d, progress=None: {
        "domain": d, "status": "LOW_PRIORITY", "facts": [], "qualification": {"total": 40}, "rounds": [], "pages": []})
    pipeline.run_batch(["x.io"])
    assert calls == [1]
