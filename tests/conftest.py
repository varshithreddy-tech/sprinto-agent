import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pytest_addoption(parser):
    parser.addoption("--pg", action="store_true",
                     help="run every test against Postgres (TEST_DATABASE_URL: the Neon ci-tests branch), "
                          "each test in its own throwaway schema")


@pytest.fixture(autouse=True)
def _never_touch_real_data(tmp_path, monkeypatch, request):
    """Every test runs against a throwaway DB and log folder, even code paths that open the default DB.
    Default: a temp SQLite file. With --pg: a fresh schema on the Neon ci-tests branch, dropped afterwards."""
    import os
    import uuid
    from config import settings as S
    from storage import db
    import agent.graph as ag
    monkeypatch.setattr(S, "DB_PATH", tmp_path / "isolated.db")
    monkeypatch.setattr(S, "DATABASE_URL", "")  # never the real Neon database
    monkeypatch.setattr(S, "DECISION_LOG_DIR", tmp_path / "decision_logs")
    monkeypatch.setattr(S, "ACTIVITY_SPOOL_PATH", tmp_path / "activity_spool.jsonl")
    schema, base = None, None
    if request.config.getoption("--pg"):
        base = os.getenv("TEST_DATABASE_URL", "")
        if not base:
            pytest.exit("--pg needs TEST_DATABASE_URL in .env", returncode=2)
        if base == os.getenv("DATABASE_URL"):
            pytest.exit("TEST_DATABASE_URL must not be the app database", returncode=2)
        import psycopg
        schema = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")
        sep = "&" if "?" in base else "?"
        monkeypatch.setattr(S, "DATABASE_URL", f"{base}{sep}options=-csearch_path%3D{schema}")
    db.set_db(None)
    ag.set_graph(None)
    yield
    cur = db._default
    db.set_db(None)
    ag.set_graph(None)
    if schema:
        try:
            if cur is not None and getattr(cur, "is_pg", False):
                cur.close()
        except Exception:
            pass
        import psycopg
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f"DROP SCHEMA {schema} CASCADE")


def pytest_sessionfinish(session, exitstatus):
    """Fail loudly if anything wrote test domains into the real database."""
    from config import settings as S
    import sqlite3
    real = ROOT / "data" / "sprinto.db"
    if not real.exists():
        return
    con = sqlite3.connect(real)
    try:
        leaked = con.execute("SELECT thread_id FROM threads WHERE thread_id IN "
                             "('acme.io','beta.io','a.io','b.io','booked.io','optout.io','rejected.io','x.io')").fetchall()
    except sqlite3.Error:
        leaked = []
    finally:
        con.close()
    if leaked:
        session.exitstatus = 1
        print(f"\nERROR: tests leaked into the real database: {leaked}")
