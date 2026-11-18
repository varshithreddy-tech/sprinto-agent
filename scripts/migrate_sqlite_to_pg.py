"""One-time copy of the local SQLite database (data/sprinto.db) into Postgres (DATABASE_URL, e.g. Neon).

Copies every table and every reply-thread's LangGraph state. Refuses to run into a database that already has
data unless --force (tables without natural keys would otherwise get duplicate rows).

    python scripts/migrate_sqlite_to_pg.py [--force]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings as S  # noqa: E402
from storage import db  # noqa: E402

TABLES = ["versions", "page_cache", "companies", "evidence", "hunter_cache", "contacts", "pains", "emails",
          "operations", "threads", "messages", "calendar_events", "activity_log", "jobs", "kv"]
SERIAL_ID = {"evidence", "contacts", "pains", "emails", "activity_log", "jobs"}


def main() -> int:
    if not S.DATABASE_URL:
        print("DATABASE_URL is not set")
        return 2
    src = sqlite3.connect(S.DB_PATH)
    src.row_factory = sqlite3.Row
    db.migrate(src)  # bring the local file to the latest schema first
    pg = db.connect()  # Postgres (creates the schema)
    busy = [t for t in ("companies", "threads", "activity_log") if db.query_one(f"SELECT 1 AS x FROM {t} LIMIT 1", conn=pg)]
    if busy and "--force" not in sys.argv:
        print(f"Target already has data in {busy}; re-run with --force to add anyway.")
        return 1
    pg_cols = {t: [r["column_name"] for r in db.query(
        "SELECT column_name FROM information_schema.columns WHERE table_name=? AND table_schema=current_schema()", (t,), pg)]
        for t in TABLES}
    for t in TABLES:
        rows = [dict(r) for r in src.execute(f"SELECT * FROM {t}" + (" ORDER BY id" if t in SERIAL_ID else ""))]
        if not rows:
            print(f"{t:16} 0")
            continue
        cols = [c for c in rows[0] if c in pg_cols[t] and not (t in SERIAL_ID and c == "id")]
        sql = f"INSERT OR IGNORE INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        pg.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        print(f"{t:16} {len(rows)}")

    # Reply-thread state: read each thread's latest checkpoint from SQLite, write it into the Postgres checkpointer.
    from langgraph.checkpoint.sqlite import SqliteSaver
    from agent.graph import build_graph, make_checkpointer
    sq_graph = build_graph(SqliteSaver(sqlite3.connect(S.DB_PATH, check_same_thread=False)))
    pg_graph = build_graph(make_checkpointer())
    n = 0
    for (tid,) in src.execute("SELECT thread_id FROM threads"):
        cfg = {"configurable": {"thread_id": tid}}
        values = sq_graph.get_state(cfg).values
        if values and not pg_graph.get_state(cfg).values:
            pg_graph.update_state(cfg, values, as_node="update_activity_log")
            n += 1
    print(f"thread states    {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
