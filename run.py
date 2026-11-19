"""CLI: python run.py --domains domains.txt [--mock-hunter] [--dry-run] [--force]
          python run.py --check-deadlines      (escalate threads past their no-reply deadline, once)
          python run.py --watch [MINUTES]      (keep checking deadlines every N minutes; Ctrl+C to stop)
          python run.py --worker               (process domains queued from the web Run page; Ctrl+C to stop)
"""
from __future__ import annotations

import argparse
import sys
import warnings

warnings.filterwarnings("ignore")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sprinto outbound agent (demo)")
    ap.add_argument("--domains", help="file with one domain per line (# comments allowed)")
    ap.add_argument("--mock-hunter", action="store_true", help="read tests/fixtures/hunter/<domain>.json instead of Hunter")
    ap.add_argument("--dry-run", action="store_true", help="do everything except the real Resend send")
    ap.add_argument("--force", action="store_true", help="re-process domains that were already sent")
    ap.add_argument("--check-deadlines", action="store_true", help="escalate threads past the no-reply deadline, once")
    ap.add_argument("--watch", nargs="?", const=-1.0, type=float, metavar="MINUTES",
                    help="check no-reply deadlines every N minutes until stopped (default DEADLINE_CHECK_MINUTES)")
    ap.add_argument("--worker", action="store_true",
                    help="research worker: process domains queued on the web app's Run page (shared database)")
    a = ap.parse_args()

    from config import settings as S

    if a.worker:
        return worker(S)

    if a.check_deadlines or a.watch is not None:
        import time
        from datetime import datetime
        from agent.graph import check_deadlines, retry_failed_logging
        minutes = S.DEADLINE_CHECK_MINUTES if (a.watch is None or a.watch < 0) else a.watch
        while True:
            lr = retry_failed_logging()
            if lr["spool_written"] or lr["threads_retried"] or lr["still_pending"]:
                print(f"activity-log retry: wrote {lr['spool_written']} spooled, retried threads {lr['threads_retried']}, "
                      f"still pending {lr['still_pending']}", flush=True)
            from outreach.records import reconcile_sends
            for r in reconcile_sends():
                print(f"send reconcile: {r.get('domain')} {r.get('kind')} -> {r['state']} ({r['method']})", flush=True)
            res = check_deadlines()
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{stamp}] deadline check: {len(res)} thread(s) escalated", flush=True)
            for r in res:
                print(f"   {r['thread']}: {r['status']} — {r['reason']}", flush=True)
            if a.watch is None:
                return 0
            try:
                time.sleep(minutes * 60)
            except KeyboardInterrupt:
                return 0
    if not a.domains:
        ap.error("--domains is required")
    missing = S.missing_keys("GEMINI_API_KEY")
    if not a.mock_hunter:
        missing += S.missing_keys("HUNTER_API_KEY")
    if not a.dry_run:
        missing += S.missing_keys("RESEND_API_KEY", "DEMO_EMAIL")
    if missing:
        print(f"Missing in .env: {', '.join(missing)}  (use --mock-hunter / --dry-run to run without them)")
        return 2
    if a.dry_run and not S.DEMO_EMAIL:
        S.DEMO_EMAIL = "demo@example.invalid"  # dry-run: guard still enforced against this placeholder

    from pipeline import run_batch
    domains = open(a.domains, encoding="utf-8").read().splitlines()
    results = run_batch(domains, mock_hunter=a.mock_hunter, dry_run=a.dry_run, force=a.force,
                        progress=lambda d, m: print(f"[{d}] {m}", flush=True))
    print("\n| domain | status | total | rounds | pages | contact | top reason |")
    print("|---|---|---|---|---|---|---|")
    for r in results:
        q = r.get("qualification") or {}
        total = q.get("total", r.get("total"))
        c = ((r.get("contact") or {}).get("selected") or {}).get("name") or r.get("contact_name") or ""
        reason = (r.get("top_reason") or "").replace("|", "/")[:110]
        print(f"| {r.get('domain')} | {r.get('status')} | {'' if total is None else f'{total:g}'} | "
              f"{r.get('additional_rounds', r.get('rounds', 0))} | {r.get('pages_fetched', '')} | {c} | {reason} |")
    return 0


def worker(S) -> int:
    """Claim queued jobs from the shared database (Neon), run the full pipeline on each, write progress back.
    Also runs maintenance (log retry, send reconciliation, no-reply deadlines) while idle."""
    import time
    from datetime import datetime, timezone
    from agent.maintenance import maybe_run_maintenance
    from pipeline import process_domain
    from storage import db, jobs

    if S.missing_keys("GEMINI_API_KEY"):
        print("Missing GEMINI_API_KEY in .env")
        return 2
    where = "Neon/Postgres" if db.using_pg() else f"SQLite {S.DB_PATH} (set DATABASE_URL to share with the web app)"
    print(f"Research worker started on {where}. Waiting for jobs from the Run page… (Ctrl+C to stop)", flush=True)
    integrations: dict = {}
    beat = lambda: db.kv_set("worker_heartbeat", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    try:
        while True:
            beat()
            jobs.requeue_stale()
            job = jobs.claim_next()
            if not job:
                maybe_run_maintenance()
                time.sleep(5)
                continue
            dry, mock = bool(job["dry_run"]), bool(job["mock_hunter"])
            print(f"[job {job['id']}] {job['domain']} ({'mock' if mock else 'live'} Hunter, {'dry run' if dry else 'real send'})", flush=True)
            need = ([] if mock else S.missing_keys("HUNTER_API_KEY")) + ([] if dry else S.missing_keys("RESEND_API_KEY", "DEMO_EMAIL"))
            if need:
                jobs.finish(job["id"], None, f"worker is missing {', '.join(need)} in .env")
                continue
            if dry and not S.DEMO_EMAIL:
                S.DEMO_EMAIL = "demo@example.invalid"

            def progress(d, msg, jid=job["id"]):
                print(f"   [{d}] {msg}", flush=True)
                jobs.progress(jid, msg)
                beat()
            try:
                res = process_domain(job["domain"], mock_hunter=mock, dry_run=dry, force=bool(job["force"]),
                                     integrations=integrations, progress=progress,
                                     run_id=f"worker-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
                if res.get("skipped"):
                    # Already drafted/sent earlier: report it as skipped, never as a new send.
                    note = f"skipped: already {str(res.get('existing_status')).replace('_', ' ').lower()} earlier — nothing re-run or sent"
                    jobs.finish(job["id"], jobs.SKIPPED, note=note)
                    print(f"[job {job['id']}] {note}", flush=True)
                else:
                    jobs.finish(job["id"], res.get("status"))
                    print(f"[job {job['id']}] done: {res.get('status')}", flush=True)
            except Exception as e:  # one domain's failure never stops the worker
                jobs.finish(job["id"], "ERROR", f"{type(e).__name__}: {e}")
                print(f"[job {job['id']}] failed: {type(e).__name__}: {e}", flush=True)
    except KeyboardInterrupt:
        print("Worker stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
