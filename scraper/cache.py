"""Page cache keyed by URL. Reused when younger than CACHE_MAX_AGE_DAYS."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from config import settings as S
from storage import db


def get(url: str) -> dict | None:
    row = db.query_one("SELECT * FROM page_cache WHERE url=? AND status='OK'", (url,))
    if not row:
        return None
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["retrieved_at"])
    if age > timedelta(days=S.CACHE_MAX_AGE_DAYS):
        return None
    row["dates"] = json.loads(row.pop("dates_json") or "[]")
    row["links"] = json.loads(row.pop("links_json") or "[]")
    row["from_cache"] = True
    return row


def put(page: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO page_cache (url, domain, status, method, text, word_count, dates_json, links_json,"
        " retrieved_at, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (page["url"], page.get("domain"), page["status"], page.get("method"), page.get("text", ""),
         page.get("word_count", 0), json.dumps(page.get("dates", [])), json.dumps(page.get("links", [])),
         page["retrieved_at"], page.get("error")))
