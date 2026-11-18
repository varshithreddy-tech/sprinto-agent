"""Email delivery via Resend — DEMO_EMAIL ONLY.

Hard guard: any recipient other than DEMO_EMAIL raises. CC/BCC are never set.
Duplicate sends are prevented by stable operation IDs stored BEFORE sending, and the same ID is passed to
Resend as an idempotency key so a retry after a timeout cannot create a second email.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Protocol

from config import settings as S
from storage import db

SENT, PENDING, UNCERTAIN, FAILED, DUPLICATE = "SENT", "PENDING", "SEND_UNCERTAIN", "SEND_FAILED", "DUPLICATE_BLOCKED"


class RecipientGuardError(Exception):
    pass


class SendTimeout(Exception):
    """Outcome unknown (timeout / connection dropped)."""


class SendAuthError(Exception):
    pass


def op_id(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def assert_demo_recipient(to: str | list[str], cc=None, bcc=None) -> str:
    demo = (S.DEMO_EMAIL or "").strip().lower()
    if not demo:
        raise RecipientGuardError("DEMO_EMAIL is not configured; refusing to send")
    recipients = [to] if isinstance(to, str) else list(to)
    if cc or bcc:
        raise RecipientGuardError("CC/BCC must be empty")
    if len(recipients) != 1 or recipients[0].strip().lower() != demo:
        raise RecipientGuardError(f"Recipient {recipients} is not DEMO_EMAIL; prospect emails are research data only")
    return demo


class EmailSender(Protocol):
    name: str

    def send(self, to: str, subject: str, text: str, attachments: list[dict] | None, idempotency_key: str) -> str: ...


class ResendSender:
    name = "resend"

    def __init__(self, api_key: str | None = None):
        import resend
        self.resend = resend
        resend.api_key = api_key or S.RESEND_API_KEY
        if not resend.api_key:
            raise SendAuthError("RESEND_API_KEY is not set (add it to .env)")

    def send(self, to, subject, text, attachments, idempotency_key):
        import requests
        assert_demo_recipient(to)
        params = {"from": S.RESEND_FROM, "to": [to], "subject": subject, "text": text}
        if attachments:
            params["attachments"] = attachments
        try:
            r = self.resend.Emails.send(params, {"idempotency_key": idempotency_key})
        except (requests.Timeout, requests.ConnectionError) as e:
            raise SendTimeout(str(e)) from e
        except (self.resend.exceptions.InvalidApiKeyError, self.resend.exceptions.MissingApiKeyError) as e:
            raise SendAuthError(str(e)) from e
        except self.resend.exceptions.ResendError as e:
            msg = str(e)
            if "only send testing emails to your own email" in msg.lower() or getattr(e, "code", None) in (401, 403):
                raise SendAuthError(f"Resend permission error: {msg}") from e
            raise
        return r["id"] if isinstance(r, dict) else getattr(r, "id")


class DryRunSender:
    """Records what would be sent; no network."""
    name = "dry-run"

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, to, subject, text, attachments, idempotency_key):
        assert_demo_recipient(to)
        self.sent.append({"to": to, "subject": subject, "text": text, "attachments": attachments, "key": idempotency_key})
        return f"dryrun-{idempotency_key[:12]}"


def get_sender(dry_run: bool) -> EmailSender:
    return DryRunSender() if dry_run else ResendSender()


def send_once(op: str, kind: str, domain: str, subject: str, text: str, sender: EmailSender,
              attachments: list[dict] | None = None, to: str | None = None) -> dict:
    """Idempotent send. Returns {"status", "provider_id", "op_id", "attempts"}."""
    to = assert_demo_recipient(to or S.DEMO_EMAIL)
    row = db.query_one("SELECT * FROM operations WHERE op_id=?", (op,))
    if row and row["status"] == SENT:
        return {"status": DUPLICATE, "provider_id": row["provider_id"], "op_id": op, "attempts": row["attempts"]}
    if row and row["provider_id"]:
        # Reconcile: a provider id was stored, so the send happened.
        db.execute("UPDATE operations SET status=?, updated_at=? WHERE op_id=?", (SENT, db.now_iso(), op))
        return {"status": DUPLICATE, "provider_id": row["provider_id"], "op_id": op, "attempts": row["attempts"]}
    attempts = row["attempts"] if row else 0
    if not row:
        db.execute("INSERT INTO operations (op_id, kind, domain, status, attempts, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                   (op, kind, domain, PENDING, 0, db.now_iso(), db.now_iso()))
    last_err = None
    while attempts < S.MAX_FETCH_ATTEMPTS:
        attempts += 1
        db.execute("UPDATE operations SET attempts=?, status=?, updated_at=? WHERE op_id=?", (attempts, PENDING, db.now_iso(), op))
        try:
            pid = sender.send(to, subject, text, attachments, op)
        except SendTimeout as e:
            # Outcome unknown. Retrying with the SAME idempotency key is safe: Resend returns the original email
            # instead of sending a second one (provider-side reconciliation).
            last_err = f"timeout: {e}"
            continue
        except (SendAuthError, RecipientGuardError) as e:
            db.execute("UPDATE operations SET status=?, detail=?, updated_at=? WHERE op_id=?", (FAILED, str(e), db.now_iso(), op))
            return {"status": FAILED, "error": str(e), "op_id": op, "attempts": attempts}
        db.execute("UPDATE operations SET status=?, provider_id=?, updated_at=? WHERE op_id=?", (SENT, pid, db.now_iso(), op))
        return {"status": SENT, "provider_id": pid, "op_id": op, "attempts": attempts}
    db.execute("UPDATE operations SET status=?, detail=?, updated_at=? WHERE op_id=?", (UNCERTAIN, last_err, db.now_iso(), op))
    return {"status": UNCERTAIN, "error": last_err, "op_id": op, "attempts": attempts}


def demo_banner(name: str, title: str, company: str) -> str:
    return f"DEMO — intended for {name}, {title} at {company}"


def new_message_id() -> str:
    return f"msg-{uuid.uuid4().hex[:16]}"
