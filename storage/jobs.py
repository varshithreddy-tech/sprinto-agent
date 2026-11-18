"""Research job queue (in the shared database).

The Vercel Run page enqueues domains; the research worker (`python run.py --worker`, on a machine that can run
Chromium for minutes at a time) claims and processes them, writing progress back for the page to show.
"""
from __future__ import annotations

import socket

from storage import db

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"


def enqueue(domains: list[str], *, mock_hunter: bool, dry_run: bool, force: bool) -> int:
    n = 0
    for d in domains:
        d = d.split("#")[0].strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
        if not d:
            continue
        db.execute("INSERT INTO jobs (domain, mock_hunter, dry_run, force, status, progress, created_at) VALUES (?,?,?,?,?,?,?)",
                   (d, int(mock_hunter), int(dry_run), int(force), QUEUED, "queued", db.now_iso()))
        n += 1
    return n


def claim_next(worker: str | None = None) -> dict | None:
    """Atomically take the oldest queued job (safe with several workers on Postgres)."""
    worker = worker or socket.gethostname()
    if db.using_pg():
        return db.query_one(
            "UPDATE jobs SET status=?, started_at=?, worker=?, progress=? WHERE id = ("
            " SELECT id FROM jobs WHERE status=? ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *",
            (RUNNING, db.now_iso(), worker, "starting", QUEUED))
    with db._LOCK:  # SQLite: single writer
        row = db.query_one("SELECT id FROM jobs WHERE status=? ORDER BY id LIMIT 1", (QUEUED,))
        if not row:
            return None
        db.execute("UPDATE jobs SET status=?, started_at=?, worker=?, progress=? WHERE id=? AND status=?",
                   (RUNNING, db.now_iso(), worker, "starting", row["id"], QUEUED))
        return db.query_one("SELECT * FROM jobs WHERE id=?", (row["id"],))


def progress(job_id: int, msg: str) -> None:
    db.execute("UPDATE jobs SET progress=? WHERE id=?", (msg[:300], job_id))


def finish(job_id: int, result_status: str | None, error: str | None = None) -> None:
    db.execute("UPDATE jobs SET status=?, result_status=?, error=?, finished_at=?, progress=? WHERE id=?",
               (FAILED if error else DONE, result_status, error, db.now_iso(), "done" if not error else "failed", job_id))


def recent(limit: int = 50) -> list[dict]:
    return db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))


def requeue_stale(minutes: int = 30) -> int:
    """Jobs left 'running' by a worker that died go back to the queue."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    cur = db.execute("UPDATE jobs SET status=?, progress=? WHERE status=? AND started_at < ?",
                     (QUEUED, "requeued: previous worker stopped", RUNNING, cutoff))
    return cur.rowcount or 0
