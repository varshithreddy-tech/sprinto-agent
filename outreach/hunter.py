"""Hunter.io Domain Search — the only contact provider. QUALIFIED companies only. Cached per domain permanently."""
from __future__ import annotations

import json
import time

import httpx

from config import settings as S
from storage import db

API = "https://api.hunter.io/v2/domain-search"


class HunterAuthError(Exception):
    """Auth/permission failure: stop the Hunter integration and surface a setup issue."""


class HunterQuotaError(Exception):
    """Quota exhausted: pause affected lookups and record the reason."""


class HunterInputError(Exception):
    """Invalid input: do not retry unchanged."""


class HunterTransientError(Exception):
    pass


def _cached(domain: str) -> dict | None:
    row = db.query_one("SELECT payload, source FROM hunter_cache WHERE domain=?", (domain,))
    return {**json.loads(row["payload"]), "_source": row["source"], "_cached": True} if row else None


def domain_search(domain: str, *, mock: bool = False, client: httpx.Client | None = None, sleep=time.sleep) -> dict:
    """Return Hunter's `data` object: {"organization":..., "emails":[...]}.

    mock=True reads tests/fixtures/hunter/<domain>.json (FICTIONAL people for offline testing).
    """
    if mock:
        path = S.FIXTURES_DIR / "hunter" / f"{domain}.json"
        if not path.exists():
            return {"organization": None, "emails": [], "_source": "mock", "_note": f"no fixture for {domain}"}
        return {**json.loads(path.read_text(encoding="utf-8"))["data"], "_source": "mock"}
    if (hit := _cached(domain)) is not None:
        return hit
    if not S.HUNTER_API_KEY:
        raise HunterAuthError("HUNTER_API_KEY is not set (add it to .env)")
    c = client or httpx.Client(timeout=20)
    last = None
    for attempt in range(1, S.MAX_FETCH_ATTEMPTS + 1):
        try:
            r = c.get(API, params={"domain": domain, "api_key": S.HUNTER_API_KEY, "limit": 10})
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last = e
            sleep(min(2 ** attempt, 8))
            continue
        body = {}
        try:
            body = r.json()
        except ValueError:
            pass
        err_ids = {e.get("id") for e in body.get("errors", [])} if isinstance(body, dict) else set()
        if r.status_code == 401 or "authentication_failed" in err_ids:
            raise HunterAuthError("Hunter rejected the API key (401)")
        if r.status_code == 429 or err_ids & {"usage_exceeded", "too_many_requests", "quota_exceeded"}:
            if "too_many_requests" in err_ids and attempt < S.MAX_FETCH_ATTEMPTS and "usage_exceeded" not in err_ids:
                sleep(min(2 ** attempt, 8))
                continue
            raise HunterQuotaError(f"Hunter quota/rate limit: {sorted(err_ids) or r.status_code}")
        if r.status_code == 403:
            raise HunterAuthError(f"Hunter permission error (403): {body.get('errors')}")
        if r.status_code in (400, 422):
            raise HunterInputError(f"Hunter rejected input: {body.get('errors')}")
        if r.status_code >= 500:
            last = HunterTransientError(f"HTTP {r.status_code}")
            sleep(min(2 ** attempt, 8))
            continue
        data = body.get("data", {})
        db.execute("INSERT OR REPLACE INTO hunter_cache (domain, payload, fetched_at, source) VALUES (?,?,?,?)",
                   (domain, json.dumps(data), db.now_iso(), "hunter"))
        return {**data, "_source": "hunter"}
    raise HunterTransientError(f"Hunter failed after {S.MAX_FETCH_ATTEMPTS} attempts: {last}")
