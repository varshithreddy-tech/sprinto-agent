"""Human review before sending: the pipeline only drafts; a reviewer edits, saves, sends once, or discards."""
import json

import pytest
from fastapi.testclient import TestClient

import pipeline
from config import settings as S
from outreach import records, send as send_mod
from storage import db

DEMO = "demo@test.local"
AI_BODY = "Hi Priya,\n\n" + " ".join(["word"] * 90) + "\n\nBest,\nSprinto Growth Team"


@pytest.fixture(autouse=True)
def demo_email(monkeypatch):
    monkeypatch.setattr(S, "DEMO_EMAIL", DEMO)


def seed(dry_run=True, domain="acme.io"):
    final = {"version": 1, "subject": "SOC 2 evidence at Acme", "body": AI_BODY, "qa_total": 9}
    log = {"domain": domain, "status": records.DRAFT_READY, "mode": {"mock_hunter": True, "dry_run": dry_run},
           "facts": [{"field": "company_name", "verified": True, "value": "Acme"}],
           "contact": {"selected": {"name": "Priya Testwal", "first_name": "Priya", "title": "CISO",
                                    "email_research_only": "priya@acme.io"}},
           "draft": records.make_draft(final, qa_passed=True)}
    records.save_log(domain, log)
    return log


def company(domain="acme.io"):
    r = db.query_one("SELECT status, decision_log FROM companies WHERE domain=?", (domain,))
    return r["status"], json.loads(r["decision_log"])


class Capture:
    name = "resend"

    def __init__(self):
        self.sent = []

    def send(self, to, subject, text, attachments, key):
        send_mod.assert_demo_recipient(to)
        self.sent.append({"to": to, "subject": subject, "text": text})
        return f"re_{len(self.sent)}"


def test_pipeline_stops_at_draft_and_never_sends(monkeypatch):
    def no_send(*a, **k):
        raise AssertionError("pipeline must not send")
    monkeypatch.setattr(send_mod, "get_sender", no_send)
    monkeypatch.setattr(send_mod, "send_once", no_send)
    monkeypatch.setattr(pipeline, "research_company", lambda d, progress=None: {
        "domain": d, "status": "QUALIFIED", "facts": [{"field": "company_name", "verified": True, "value": "Acme", "label": "observed",
                                                       "source_url": "https://acme.io", "exact_excerpt": "Acme"}],
        "qualification": {"total": 80, "dimensions": {}}, "rounds": [], "pages": []})
    monkeypatch.setattr(pipeline.hunter, "domain_search", lambda d, mock=False: {"emails": []})
    monkeypatch.setattr(pipeline.contact_mod, "select", lambda *a, **k: {
        "status": "CONTACT_SELECTED", "candidates": [],
        "selected": {"name": "Priya Testwal", "first_name": "Priya", "title": "CISO"}})
    monkeypatch.setattr(pipeline.pains_mod, "generate", lambda *a, **k: {
        "status": "PAINS_OK", "fail_reasons": [], "llm": {}, "hypotheses": [
            {"score": 9, "hypothesis": "h1", "passed": True}, {"score": 8, "hypothesis": "h2", "passed": True}]})
    monkeypatch.setattr(pipeline.email_qa, "write_and_qa", lambda *a, **k: {
        "status": "EMAIL_QA_PASSED", "final_version": 1, "versions": [
            {"version": 1, "subject": "S", "body": AI_BODY, "qa_total": 9, "qa_scores": {}, "qa_checks": {}, "passed": True}]})
    log = pipeline.process_domain("acme.io", mock_hunter=True, dry_run=False)
    assert log["status"] == "DRAFT_READY" and log["draft"]["state"] == "pending_review"
    assert db.query("SELECT 1 FROM operations") == [] and db.query("SELECT 1 FROM threads") == []
    # a re-run doesn't regenerate (and overwrite) the waiting draft
    assert pipeline.process_domain("acme.io", mock_hunter=True)["status"] == "DRAFT_READY"


def test_qa_failed_keeps_best_draft_for_review(monkeypatch):
    monkeypatch.setattr(pipeline, "research_company", lambda d, progress=None: {
        "domain": d, "status": "QUALIFIED", "facts": [], "qualification": {"total": 80}, "rounds": [], "pages": []})
    monkeypatch.setattr(pipeline.hunter, "domain_search", lambda d, mock=False: {"emails": []})
    monkeypatch.setattr(pipeline.contact_mod, "select", lambda *a, **k: {
        "status": "CONTACT_SELECTED", "candidates": [], "selected": {"name": "P T", "first_name": "P", "title": "CISO"}})
    monkeypatch.setattr(pipeline.pains_mod, "generate", lambda *a, **k: {
        "status": "PAINS_OK", "fail_reasons": [], "llm": {}, "hypotheses": [{"score": 9, "hypothesis": "h", "passed": True}]})
    vs = [{"version": i, "subject": f"S{i}", "body": f"B{i}", "qa_total": t, "qa_scores": {}, "qa_checks": {}, "passed": False}
          for i, t in ((1, 5), (2, 7), (3, 6))]
    monkeypatch.setattr(pipeline.email_qa, "write_and_qa", lambda *a, **k: {"status": "EMAIL_QA_FAILED", "final_version": None, "versions": vs})
    log = pipeline.process_domain("beta.io", mock_hunter=True)
    assert log["status"] == "EMAIL_QA_FAILED" and log["draft"]["version"] == 2 and log["draft"]["qa_passed"] is False


def test_edit_save_then_send_once(monkeypatch):
    cap = Capture()
    monkeypatch.setattr(send_mod, "get_sender", lambda dry: cap)
    seed(dry_run=False)
    records.save_draft("acme.io", "SOC 2 at Acme (edited)", AI_BODY.replace("word word", "evidence collection", 1))
    _, log = company()
    assert log["draft"]["edited"] is True and log["draft"]["ai_subject"] == "SOC 2 evidence at Acme"
    assert db.query("SELECT 1 FROM operations") == []                        # saving never sends
    res = records.send_draft("acme.io", log["draft"]["subject"], log["draft"]["body"])
    assert res["status"] == "SENT" and len(cap.sent) == 1
    m = cap.sent[0]
    assert m["to"] == DEMO and m["subject"] == "SOC 2 at Acme (edited)"
    assert m["text"].startswith("DEMO — intended for Priya Testwal, CISO at Acme") and "evidence collection" in m["text"]
    status, log = company()
    assert status == "EMAIL_SENT" and log["draft"]["state"] == "sent" and log["send"]["edited_by_reviewer"] is True
    assert db.query_one("SELECT status FROM threads WHERE thread_id='acme.io'")["status"] == "AWAITING_REPLY"
    with pytest.raises(ValueError):                                          # second send impossible
        records.send_draft("acme.io", "x", "y")
    assert len(cap.sent) == 1


def test_discard_blocks_sending(monkeypatch):
    seed()
    records.discard_draft("acme.io")
    status, log = company()
    assert status == "DRAFT_DISCARDED" and log["draft"]["state"] == "discarded"
    with pytest.raises(ValueError):
        records.send_draft("acme.io", "S", "B")
    assert db.query("SELECT 1 FROM operations") == []


def test_checks_flag_problems_in_edits():
    checks = {c["check"].split(" (")[0]: c["ok"] for c in records.draft_checks("Re: quick one", "Hello there,\nfollowing up", "Priya")}
    assert checks["Addressed to the recipient by name"] is False
    assert checks["No false “Re:”/“Fwd:” subject"] is False
    assert checks["No fake “following up” language"] is False


def test_web_review_flow(monkeypatch):
    monkeypatch.setattr(S, "PUBLIC_ACCESS", True)
    from sprinto_web.app import app
    c = TestClient(app)
    seed(dry_run=True)
    assert "acme.io" in c.get("/drafts").text
    page = c.get("/company/acme.io").text
    assert "Review draft before sending" in page and "Send email" in page
    r = c.post("/company/acme.io/draft", data={"action": "save", "subject": "New subject", "body": AI_BODY},
               follow_redirects=False)
    assert "Draft%20saved" in r.headers["location"] or "Draft saved" in r.headers["location"]
    r = c.post("/company/acme.io/draft", data={"action": "send", "subject": "New subject", "body": AI_BODY},
               follow_redirects=False)
    assert r.headers["location"] == "/threads/acme.io"
    status, log = company()
    assert status == "DRY_RUN_SENT" and log["send"]["provider_id"].startswith("dryrun-")
    assert "acme.io" not in c.get("/drafts").text
    assert "Sent after review" in c.get("/company/acme.io").text


def test_rerun_of_sent_company_is_reported_as_skipped(monkeypatch):
    """A job for a company emailed earlier must not look like a new send."""
    def no_send(*a, **k):
        raise AssertionError("must not send")
    monkeypatch.setattr(send_mod, "get_sender", no_send)
    db.upsert_company("acme.io", status="EMAIL_SENT", total=80, decision_log="{}")
    res = pipeline.process_domain("acme.io", mock_hunter=True, dry_run=True)
    assert res["skipped"] is True and res["existing_status"] == "EMAIL_SENT"
    from storage import jobs
    jobs.enqueue(["acme.io"], mock_hunter=True, dry_run=True, force=False)
    j = jobs.claim_next("test")
    jobs.finish(j["id"], jobs.SKIPPED, note="skipped: already email sent earlier — nothing re-run or sent")
    row = db.query_one("SELECT result_status, progress FROM jobs WHERE id=?", (j["id"],))
    assert row["result_status"] == "SKIPPED" and "nothing re-run or sent" in row["progress"]
