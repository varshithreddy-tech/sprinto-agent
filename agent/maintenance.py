"""Periodic upkeep shared by the dashboard watcher, the worker, `run.py --watch` and the Vercel app:
retry failed activity-log writes, reconcile uncertain sends, escalate threads past the no-reply deadline."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from config import settings as S
from storage import db


def run_maintenance() -> dict:
    from agent.graph import check_deadlines, retry_failed_logging
    from outreach.records import reconcile_sends
    out: dict = {"at": datetime.now(timezone.utc).isoformat()}
    for name, fn in (("log_retry", retry_failed_logging), ("reconcile", reconcile_sends), ("deadlines", check_deadlines)):
        try:
            out[name] = fn()
        except Exception as e:  # one failing step never blocks the others
            out[name] = {"error": f"{type(e).__name__}: {e}"}
    try:
        db.kv_set("last_maintenance", json.dumps(out, default=str))
    except Exception:
        pass
    return out


def maybe_run_maintenance(min_interval_minutes: float | None = None) -> dict | None:
    """Run maintenance only if the last run is older than the interval (cheap check for page loads)."""
    interval = timedelta(minutes=min_interval_minutes if min_interval_minutes is not None else S.DEADLINE_CHECK_MINUTES)
    last = last_maintenance()
    if last and datetime.now(timezone.utc) - datetime.fromisoformat(last["at"]) < interval:
        return None
    return run_maintenance()


def last_maintenance() -> dict | None:
    raw = db.kv_get("last_maintenance")
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
