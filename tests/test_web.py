"""Web dashboard: login vs public mode, email redaction, rate limits (FastAPI TestClient, temp SQLite)."""
import json

import pytest
from fastapi.testclient import TestClient

from config import settings as S
from storage import db


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(S, "APP_PASSWORD", "test-pass")
    monkeypatch.setattr(S, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(S, "PUBLIC_ACCESS", False)
    from sprinto_web.app import app
    return TestClient(app)


def seed_company():
    log = {"domain": "acme.io", "status": "EMAIL_SENT",
           "send": {"to": "owner@school.example", "intended_for": "Priya <research-only: priya@acme.io>"}}
    db.upsert_company("acme.io", status="EMAIL_SENT", total=80, decision_log=json.dumps(log))


def test_private_mode_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.post("/run", data={"domains": "x.io"}, follow_redirects=False).status_code == 401
    r = client.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/").status_code == 200


def test_public_mode_opens_everything_but_cron(client, monkeypatch):
    monkeypatch.setattr(S, "PUBLIC_ACCESS", True)
    monkeypatch.setattr(S, "CRON_SECRET", "cron-secret")
    seed_company()
    for path in ("/", "/company/acme.io", "/run", "/threads", "/calendar", "/activity"):
        assert client.get(path, follow_redirects=False).status_code == 200, path
    assert "Log out" not in client.get("/").text
    assert client.get("/tasks/cron").status_code == 401                      # still needs the secret
    assert client.get("/healthz").json()["public"] is True


def test_public_pages_hide_email_addresses(client, monkeypatch):
    monkeypatch.setattr(S, "PUBLIC_ACCESS", True)
    seed_company()
    html = client.get("/company/acme.io").text
    assert "owner@school.example" not in html and "priya@acme.io" not in html
    assert "[email hidden]" in html


def test_public_queue_and_rate_limit(client, monkeypatch):
    monkeypatch.setattr(S, "PUBLIC_ACCESS", True)
    monkeypatch.setattr(S, "RATE_LIMIT_DOMAINS_PER_HOUR", 3)
    r = client.post("/run", data={"domains": "a.io\nb.io", "mock_hunter": "on", "dry_run": "on"}, follow_redirects=False)
    assert r.headers["location"] == "/run?queued=2"
    r = client.post("/run", data={"domains": "c.io\nd.io"}, follow_redirects=False)
    assert r.headers["location"] == "/run?limited=1"                          # 2 + 2 > 3
    assert [j["domain"] for j in db.query("SELECT domain FROM jobs ORDER BY id")] == ["a.io", "b.io"]
    jobs = db.query("SELECT mock_hunter, dry_run FROM jobs")
    assert all(j["mock_hunter"] == 1 and j["dry_run"] == 1 for j in jobs)


def test_reply_rate_limit(client, monkeypatch):
    monkeypatch.setattr(S, "PUBLIC_ACCESS", True)
    monkeypatch.setattr(S, "RATE_LIMIT_REPLIES_PER_HOUR", 0)
    r = client.post("/threads/acme.io/reply", data={"body": "hi"}, follow_redirects=False)
    assert "Rate%20limit" in r.headers["location"] or "Rate limit" in r.headers["location"]
