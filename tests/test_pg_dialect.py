"""SQLite -> Postgres SQL translation used by storage.db (pure, no database)."""
import pytest

from storage.db import to_pg


def test_placeholders_and_literal_percent():
    assert to_pg("SELECT * FROM t WHERE a=? AND b LIKE 'x-%'") == "SELECT * FROM t WHERE a=%s AND b LIKE 'x-%%'"


def test_insert_or_ignore():
    assert to_pg("INSERT OR IGNORE INTO messages (a, b) VALUES (?,?)") == \
        "INSERT INTO messages (a, b) VALUES (%s,%s) ON CONFLICT DO NOTHING"


def test_insert_or_replace_becomes_upsert():
    out = to_pg("INSERT OR REPLACE INTO hunter_cache (domain, payload, fetched_at, source) VALUES (?,?,?,?)")
    assert out == ("INSERT INTO hunter_cache (domain, payload, fetched_at, source) VALUES (%s,%s,%s,%s) "
                   "ON CONFLICT (domain) DO UPDATE SET payload=EXCLUDED.payload, fetched_at=EXCLUDED.fetched_at, "
                   "source=EXCLUDED.source")


def test_insert_or_replace_without_columns_is_refused():
    with pytest.raises(ValueError):
        to_pg("INSERT OR REPLACE INTO hunter_cache VALUES (?,?,?,?)")


def test_plain_sql_untouched_apart_from_params():
    assert to_pg("UPDATE kv SET value=? WHERE key=?") == "UPDATE kv SET value=%s WHERE key=%s"
