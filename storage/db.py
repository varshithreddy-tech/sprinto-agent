"""SQLite schema + tiny migration runner + helpers.

LangGraph checkpoints live in the same file (tables created by SqliteSaver).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings

_LOCK = threading.RLock()

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS versions (
        kind TEXT, name TEXT, version TEXT, content TEXT, recorded_at TEXT,
        PRIMARY KEY (kind, name, version));
    CREATE TABLE IF NOT EXISTS page_cache (
        url TEXT PRIMARY KEY, domain TEXT, status TEXT, method TEXT, text TEXT,
        word_count INTEGER, dates_json TEXT, links_json TEXT, retrieved_at TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS companies (
        domain TEXT PRIMARY KEY, run_id TEXT, status TEXT, total INTEGER, rounds INTEGER,
        pages_fetched INTEGER, contact_name TEXT, contact_title TEXT, email_status TEXT,
        top_reason TEXT, escalation_reason TEXT, rubric_version TEXT,
        decision_log TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, field TEXT, value TEXT, label TEXT,
        source_url TEXT, exact_excerpt TEXT, retrieval_date TEXT, publication_date TEXT,
        verified INTEGER, round INTEGER);
    CREATE TABLE IF NOT EXISTS hunter_cache (
        domain TEXT PRIMARY KEY, payload TEXT, fetched_at TEXT, source TEXT);
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, name TEXT, title TEXT, email TEXT,
        role_category INTEGER, scores_json TEXT, total INTEGER, passed INTEGER, selected INTEGER,
        reasons_json TEXT);
    CREATE TABLE IF NOT EXISTS pains (
        id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, idx INTEGER, data_json TEXT,
        score INTEGER, passed INTEGER, prompt_version TEXT);
    CREATE TABLE IF NOT EXISTS emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, version INTEGER, subject TEXT,
        body TEXT, qa_json TEXT, qa_score INTEGER, passed INTEGER, created_at TEXT);
    CREATE TABLE IF NOT EXISTS operations (
        op_id TEXT PRIMARY KEY, kind TEXT, domain TEXT, status TEXT, provider_id TEXT,
        attempts INTEGER DEFAULT 0, detail TEXT, created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS threads (
        thread_id TEXT PRIMARY KEY, domain TEXT, contact_name TEXT, contact_title TEXT,
        company TEXT, subject TEXT, created_at TEXT, reply_deadline TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS messages (
        message_id TEXT PRIMARY KEY, thread_id TEXT, direction TEXT, author TEXT,
        subject TEXT, body TEXT, provider_id TEXT, created_at TEXT, simulated INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS calendar_events (
        event_id TEXT PRIMARY KEY, kind TEXT, title TEXT, start_utc TEXT, end_utc TEXT,
        attendee TEXT, domain TEXT, status TEXT, op_id TEXT, created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, domain TEXT, event TEXT, detail TEXT);
    """,
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or settings.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    with _LOCK:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version={i}")
        conn.commit()


_default: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _default
    if _default is None:
        _default = connect()
    return _default


def set_db(conn: sqlite3.Connection | None) -> None:
    """Tests swap in a temp database."""
    global _default
    _default = conn


def execute(sql: str, params: tuple | dict = (), conn: sqlite3.Connection | None = None) -> sqlite3.Cursor:
    c = conn or get_db()
    with _LOCK:
        cur = c.execute(sql, params)
        c.commit()
        return cur


def query(sql: str, params: tuple | dict = (), conn: sqlite3.Connection | None = None) -> list[dict]:
    c = conn or get_db()
    with _LOCK:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | dict = (), conn=None) -> dict | None:
    rows = query(sql, params, conn)
    return rows[0] if rows else None


def record_version(kind: str, name: str, version: str, content: str = "") -> None:
    execute("INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?)", (kind, name, version, content, now_iso()))


def log_activity(domain: str, event: str, detail: Any = None) -> None:
    """DEMO ACTIVITY LOG — not a real CRM."""
    execute("INSERT INTO activity_log (ts, domain, event, detail) VALUES (?,?,?,?)",
            (now_iso(), domain, event, json.dumps(detail, default=str) if detail is not None else None))


def upsert_company(domain: str, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    existing = query_one("SELECT domain FROM companies WHERE domain=?", (domain,))
    if existing:
        sets = ", ".join(f"{k}=?" for k in fields)
        execute(f"UPDATE companies SET {sets} WHERE domain=?", (*fields.values(), domain))
    else:
        cols = ", ".join(["domain", *fields])
        qs = ", ".join("?" * (len(fields) + 1))
        execute(f"INSERT INTO companies ({cols}) VALUES ({qs})", (domain, *fields.values()))
