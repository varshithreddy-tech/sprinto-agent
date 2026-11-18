"""Failure handling (rubric 3.11): timeout, 403, Hunter quota, duplicate send, restart resume, batch isolation."""
from __future__ import annotations

import httpx
import pytest

import pipeline
from agent import state as st
from agent.graph import get_state
from config import settings as S
from outreach import hunter, send as send_mod
from scraper import robots
from scraper.fetch import FAILED, OK, Fetcher
from storage import db
from tests.helpers import DEMO, Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(robots, "allowed", lambda url: True)
    yield e
    e.close()


def fetcher_with(handler, calls):
    def wrapped(request):
        calls.append(str(request.url))
        return handler(request)
    client = httpx.Client(transport=httpx.MockTransport(wrapped), follow_redirects=True)
    return Fetcher("example.com", client=client, use_playwright=False, use_cache=False, sleep=lambda s: None)


def test_transient_timeout_retries_three_times_then_fails(env):
    calls = []

    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)
    p = fetcher_with(boom, calls).fetch("https://example.com/security")
    assert p["status"] == FAILED and "transient failure after 3 attempts" in p["error"]
    assert len(calls) == 3 and p["attempts"] == 3


def test_timeout_then_success_recovers(env):
    calls = []
    html = "<html><body><main>" + "".join(f"<p>Paragraph {i}: we are a cloud software platform for enterprise customers with SOC 2 Type II audits.</p>" for i in range(30)) + "</main></body></html>"

    def flaky(request):
        if len(calls) <= 2:  # first two attempts fail
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})
    p = fetcher_with(flaky, calls).fetch("https://example.com/")
    assert p["status"] == OK and p["attempts"] == 3 and p["word_count"] > 100


def test_403_is_retrieval_failure_not_retried(env):
    calls = []
    p = fetcher_with(lambda r: httpx.Response(403, text="Forbidden"), calls).fetch("https://example.com/trust")
    assert p["status"] == FAILED and "403" in p["error"] and len(calls) == 1


def test_captcha_not_bypassed(env):
    calls = []
    page = "<html><title>Just a moment...</title><div class='cf-challenge'>Verify you are human</div></html>"
    p = fetcher_with(lambda r: httpx.Response(200, text=page), calls).fetch("https://example.com/")
    assert p["status"] == FAILED and "CAPTCHA" in p["error"]


def test_homepage_403_routes_to_retrieval_failed_not_poor_fit(env, monkeypatch):
    from research.loop import research_company
    calls = []
    f = fetcher_with(lambda r: httpx.Response(403, text="nope"), calls)
    log = research_company("example.com", fetcher=f)
    assert log["status"] == "RETRIEVAL_FAILED" and "not poor fit" in log["top_reason"]


def test_hunter_quota_exhausted_pauses(env, monkeypatch):
    monkeypatch.setattr(S, "HUNTER_API_KEY", "test-key")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, json={"errors": [{"id": "usage_exceeded", "details": "quota"}]})))
    with pytest.raises(hunter.HunterQuotaError):
        hunter.domain_search("acme.io", client=client, sleep=lambda s: None)


def test_hunter_auth_error(env, monkeypatch):
    monkeypatch.setattr(S, "HUNTER_API_KEY", "bad")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"errors": [{"id": "authentication_failed"}]})))
    with pytest.raises(hunter.HunterAuthError):
        hunter.domain_search("acme.io", client=client, sleep=lambda s: None)


def test_pipeline_hunter_quota_pauses_only_lookups(env, monkeypatch):
    """Quota on account 1 -> HUNTER_QUOTA_PAUSED; account 2 is paused without another API call; batch continues."""
    qualified = {"domain": None, "status": "QUALIFIED", "facts": [], "qualification": {"total": 80}, "rounds": [], "pages": []}
    monkeypatch.setattr(pipeline, "research_company", lambda d, progress=None: {**qualified, "domain": d})
    calls = []

    def quota(domain, mock=False):
        calls.append(domain)
        raise hunter.HunterQuotaError("usage_exceeded")
    monkeypatch.setattr(pipeline.hunter, "domain_search", quota)
    res = pipeline.run_batch(["a.io", "b.io"])
    assert [r["status"] for r in res] == ["HUNTER_QUOTA_PAUSED", "HUNTER_PAUSED"]
    assert calls == ["a.io"]


def test_one_account_failure_does_not_stop_batch(env, monkeypatch):
    def research(d, progress=None):
        if d == "bad.io":
            raise RuntimeError("boom")
        return {"domain": d, "status": "LOW_PRIORITY", "facts": [], "qualification": {"total": 40}, "rounds": [], "pages": []}
    monkeypatch.setattr(pipeline, "research_company", research)
    res = pipeline.run_batch(["bad.io", "good.io"])
    assert res[0]["status"] == "ERROR" and res[1]["status"] == "LOW_PRIORITY"


def test_duplicate_send_blocked(env):
    op = send_mod.op_id("acme.io", "initial-email", 1, "dry-run")
    r1 = send_mod.send_once(op, "initial_email", "acme.io", "Subject", "Body", env.sender)
    r2 = send_mod.send_once(op, "initial_email", "acme.io", "Subject", "Body", env.sender)
    assert r1["status"] == send_mod.SENT and r2["status"] == send_mod.DUPLICATE
    assert len(env.sender.sent) == 1


def test_send_timeout_reconciles_with_same_idempotency_key(env):
    class Flaky:
        name = "flaky"
        keys = []

        def send(self, to, subject, text, attachments, idempotency_key):
            self.keys.append(idempotency_key)
            if len(self.keys) == 1:
                raise send_mod.SendTimeout("read timeout")
            return "re_123"
    s = Flaky()
    r = send_mod.send_once("op-timeout", "initial_email", "acme.io", "S", "B", s)
    assert r["status"] == send_mod.SENT and r["provider_id"] == "re_123"
    assert s.keys == ["op-timeout", "op-timeout"]  # same key -> provider dedupes


def test_send_uncertain_after_repeated_timeouts(env):
    class Dead:
        name = "dead"

        def send(self, *a, **k):
            raise send_mod.SendTimeout("timeout")
    r = send_mod.send_once("op-dead", "initial_email", "acme.io", "S", "B", Dead())
    assert r["status"] == send_mod.UNCERTAIN
    assert db.query_one("SELECT status FROM operations WHERE op_id='op-dead'")["status"] == "SEND_UNCERTAIN"


def test_recipient_guard(env):
    with pytest.raises(send_mod.RecipientGuardError):
        send_mod.assert_demo_recipient("prospect@acme.io")
    with pytest.raises(send_mod.RecipientGuardError):
        send_mod.assert_demo_recipient(DEMO, cc=["x@y.z"])
    with pytest.raises(send_mod.RecipientGuardError):
        env.sender.send("ciso@acme.io", "s", "b", None, "k")
    assert send_mod.assert_demo_recipient(DEMO.upper()) == DEMO


def test_restart_resumes_from_checkpoint(env):
    tid = env.open_thread()
    s = env.reply(tid, "interested", mid="m-1")
    proposed = s["times_proposed"]
    # simulate a process restart: brand-new graph object + new checkpointer connection on the same DB
    env.graph = env.new_graph()
    restored = get_state(tid, graph=env.graph)
    assert restored["reply_status"] == st.TIMES_PROPOSED and restored["times_proposed"] == proposed
    assert "m-1" in restored["processed_message_ids"]
    again = env.reply(tid, "interested", mid="m-1")          # replayed message is not reprocessed
    assert "already processed" in again["skip_reason"]
    s = env.reply(tid, "Option 1 works", mid="m-2")             # conversation continues from persisted state
    assert s["reply_status"] == st.MEETING_BOOKED and s["confirmed_slot"] == proposed[0]


def test_calendar_failure_escalates_never_proposes_unchecked(env, monkeypatch):
    from agent import calendar_provider as cp

    class Broken(cp.DemoCalendar):
        def busy(self, *a, **k):
            raise RuntimeError("calendar API down")
    cp.set_calendar(Broken(env.conn))
    tid = env.open_thread()
    s = env.reply(tid, "interested")
    assert s["reply_status"] == st.ESCALATED and "Calendar check failed" in s["escalation_reason"]
    assert s["times_proposed"] == [] and env.sender.sent == []


def test_logging_failure_preserves_actions(env, monkeypatch):
    from agent import tools as T
    monkeypatch.setattr(T.db, "log_entries", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log db locked")))
    tid = env.open_thread()
    s = env.reply(tid, "interested")
    assert s["reply_status"] == st.TIMES_PROPOSED and len(env.sender.sent) == 1  # send preserved
    assert s["crm_logged"] is False and s["pending_log"]                        # logging retried later
