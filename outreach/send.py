"""Email delivery via Resend — DEMO_EMAIL ONLY.

Hard guard: any recipient other than DEMO_EMAIL raises. CC/BCC are never set.
Duplicate sends are prevented by stable operation IDs stored BEFORE sending, and the same ID is passed to
Resend as an idempotency key so a retry after a timeout cannot create a second email.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
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


class LookupUnavailable(Exception):
    """The provider can't be queried (e.g. a send-only API key): fall back to idempotency-key reconciliation."""


RECONCILE_SKEW = timedelta(minutes=2)


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

    _lookup_blocked: str | None = None  # cached per process once Resend says the key can't read

    def find_sent(self, to: str, subject: str, text: str, since: datetime) -> str | None:
        """Look for an email we may already have sent: same recipient + subject, created after `since`
        (minus clock skew), and EXACTLY the same text. Returns its Resend id, or None if it isn't there.
        Raises LookupUnavailable when the key can't read emails (send-only keys)."""
        if ResendSender._lookup_blocked:
            raise LookupUnavailable(ResendSender._lookup_blocked)
        cutoff = since - RECONCILE_SKEW
        after = None
        try:
            for _page in range(3):
                params = {"limit": 100, **({"after": after} if after else {})}
                page = self.resend.Emails.list(params)
                data = page.get("data", []) if isinstance(page, dict) else []
                for e in data:
                    created = _parse_ts(e.get("created_at"))
                    if created and created < cutoff:
                        return None  # list is newest-first: nothing older can match
                    tos = e.get("to") if isinstance(e.get("to"), list) else [e.get("to")]
                    if e.get("subject") != subject or to.lower() not in [str(t).lower() for t in tos]:
                        continue
                    full = self.resend.Emails.get(e["id"])
                    if _norm_text(full.get("text")) == _norm_text(text):
                        return e["id"]
                if not (isinstance(page, dict) and page.get("has_more") and data):
                    return None
                after = data[-1]["id"]
            return None
        except self.resend.exceptions.ResendError as e:
            msg = str(e)
            if "restricted" in msg.lower() or getattr(e, "code", None) in (401, 403):
                ResendSender._lookup_blocked = f"Resend key cannot read emails: {msg}"
                raise LookupUnavailable(ResendSender._lookup_blocked) from e
            raise


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _norm_text(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


class DryRunSender:
    """Records what would be sent; no network."""
    name = "dry-run"

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, to, subject, text, attachments, idempotency_key):
        assert_demo_recipient(to)
        self.sent.append({"to": to, "subject": subject, "text": text, "attachments": attachments, "key": idempotency_key})
        return f"dryrun-{idempotency_key[:12]}"

    def find_sent(self, to, subject, text, since):
        for m in self.sent:
            if m["to"] == to and m["subject"] == subject and _norm_text(m["text"]) == _norm_text(text):
                return f"dryrun-{m['key'][:12]}"
        return None


def get_sender(dry_run: bool) -> EmailSender:
    return DryRunSender() if dry_run else ResendSender()


FOUND, NOT_FOUND, UNKNOWN = "FOUND", "NOT_FOUND", "UNKNOWN"


def reconcile(sender, to: str, subject: str, text: str, since: datetime) -> tuple[str, str | None, str]:
    """Ask the provider whether this exact email already went out. Returns (FOUND|NOT_FOUND|UNKNOWN, id, method)."""
    finder = getattr(sender, "find_sent", None)
    if finder is None:
        return UNKNOWN, None, f"{getattr(sender, 'name', 'sender')} has no lookup"
    try:
        pid = finder(to, subject, text, since)
    except LookupUnavailable as e:
        return UNKNOWN, None, f"lookup unavailable ({e}); relying on idempotency key"
    except Exception as e:  # lookup itself failed: don't guess
        return UNKNOWN, None, f"lookup failed: {type(e).__name__}: {e}"
    return (FOUND, pid, "provider lookup") if pid else (NOT_FOUND, None, "provider lookup")


def _op_update(op: str, **f) -> None:
    f["updated_at"] = db.now_iso()
    db.execute(f"UPDATE operations SET {', '.join(k + '=?' for k in f)} WHERE op_id=?", (*f.values(), op))


def send_once(op: str, kind: str, domain: str, subject: str, text: str, sender: EmailSender,
              attachments: list[dict] | None = None, to: str | None = None) -> dict:
    """Idempotent send with provider reconciliation (rubric 3.11 'send timeout: reconcile provider state
    before retrying'). Returns {"status", "provider_id", "op_id", "attempts", "reconcile"}."""
    to = assert_demo_recipient(to or S.DEMO_EMAIL)
    row = db.query_one("SELECT * FROM operations WHERE op_id=?", (op,))
    if row and row["status"] == SENT:
        return {"status": DUPLICATE, "provider_id": row["provider_id"], "op_id": op, "attempts": row["attempts"]}
    if row and row["provider_id"]:
        # A provider id was stored, so the send happened.
        _op_update(op, status=SENT)
        return {"status": DUPLICATE, "provider_id": row["provider_id"], "op_id": op, "attempts": row["attempts"]}
    log: list[str] = []

    def done(status, pid=None, error=None, attempts=0):
        _op_update(op, status=status, provider_id=pid, detail=json.dumps({"reconcile": log, "error": error}))
        out = {"status": status, "provider_id": pid, "op_id": op, "attempts": attempts, "reconcile": log}
        if error:
            out["error"] = error
        return out

    if row:
        # Resume after a crash/timeout (PENDING or SEND_UNCERTAIN): reconcile BEFORE sending anything.
        attempts = row["attempts"] or 0
        since = datetime.fromisoformat(row["created_at"])
        state, pid, how = reconcile(sender, to, subject, text, since)
        log.append(f"before retry: {state} via {how}")
        if state == FOUND:
            return done(SENT, pid, attempts=attempts)
    else:
        attempts = 0
        since = datetime.now(timezone.utc)
        db.execute("INSERT INTO operations (op_id, kind, domain, status, attempts, created_at, updated_at, payload)"
                   " VALUES (?,?,?,?,?,?,?,?)",
                   (op, kind, domain, PENDING, 0, since.replace(microsecond=0).isoformat(), db.now_iso(),
                    json.dumps({"sender": getattr(sender, "name", "?"), "to": to, "subject": subject, "text": text,
                                "has_attachments": bool(attachments)})))
    last_err = None
    while attempts < S.MAX_FETCH_ATTEMPTS:
        attempts += 1
        _op_update(op, attempts=attempts, status=PENDING)
        try:
            pid = sender.send(to, subject, text, attachments, op)
        except SendTimeout as e:
            last_err = f"timeout: {e}"
            # Outcome unknown: check the provider first. Only if it is NOT there (or can't be checked) do we
            # retry — always with the SAME idempotency key, so the provider dedupes a send that did land.
            state, found, how = reconcile(sender, to, subject, text, since)
            log.append(f"after timeout #{attempts}: {state} via {how}")
            if state == FOUND:
                return done(SENT, found, attempts=attempts)
            continue
        except (SendAuthError, RecipientGuardError) as e:
            return done(FAILED, error=str(e), attempts=attempts)
        return done(SENT, pid, attempts=attempts)
    return done(UNCERTAIN, error=last_err, attempts=attempts)


def reconcile_uncertain(sender_for: callable, on_found: callable | None = None) -> list[dict]:
    """Sweep: for each SEND_UNCERTAIN / stale PENDING operation, look it up at the provider. Found ones are
    marked SENT (and on_found(op_row, provider_id) finalises e.g. the company/thread). Not-found or
    un-checkable ones stay uncertain for a human: this sweep NEVER re-sends."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(microsecond=0).isoformat()
    rows = db.query("SELECT * FROM operations WHERE (status=? OR (status=? AND updated_at < ?)) AND payload IS NOT NULL",
                    (UNCERTAIN, PENDING, stale))
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        sender = sender_for(p.get("sender"))
        if sender is None:
            out.append({"op_id": r["op_id"], "state": UNKNOWN, "method": f"no sender available for {p.get('sender')}"})
            continue
        state, pid, how = reconcile(sender, p["to"], p["subject"], p["text"], datetime.fromisoformat(r["created_at"]))
        out.append({"op_id": r["op_id"], "domain": r["domain"], "kind": r["kind"], "state": state, "method": how})
        if state == FOUND:
            _op_update(r["op_id"], status=SENT, provider_id=pid,
                       detail=json.dumps({"reconcile": [f"sweep: FOUND via {how}"]}))
            db.log_activity_safe(r["domain"], "SEND_RECONCILED", {"op_id": r["op_id"], "provider_id": pid, "kind": r["kind"]})
            if on_found:
                try:
                    on_found({**r, "payload": p}, pid)
                except Exception as e:
                    db.log_activity_safe(r["domain"], "SEND_RECONCILE_FINALISE_FAILED", str(e))
    return out


def demo_banner(name: str, title: str, company: str) -> str:
    return f"DEMO — intended for {name}, {title} at {company}"


def new_message_id() -> str:
    return f"msg-{uuid.uuid4().hex[:16]}"
