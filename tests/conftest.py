import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _never_touch_real_data(tmp_path, monkeypatch):
    """Every test runs against a throwaway DB and log folder, even code paths that open the default DB."""
    from config import settings as S
    from storage import db
    import agent.graph as ag
    monkeypatch.setattr(S, "DB_PATH", tmp_path / "isolated.db")
    monkeypatch.setattr(S, "DECISION_LOG_DIR", tmp_path / "decision_logs")
    db.set_db(None)
    ag.set_graph(None)
    yield
    db.set_db(None)
    ag.set_graph(None)


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
