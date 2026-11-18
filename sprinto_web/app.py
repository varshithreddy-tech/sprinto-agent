"""Web dashboard (FastAPI). Runs on Vercel (api/index.py) and locally: `uvicorn sprinto_web.app:app --reload`.

Same data as the Streamlit dashboard, read from the shared database (Neon). The reply agent and calendar run here
live; long research runs are queued for the worker (`python run.py --worker`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import settings as S
from storage import db, jobs

app = FastAPI(title="Sprinto outbound agent (demo)", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
IST = ZoneInfo(S.ORGANIZER_TZ)
COOKIE = "sprinto_session"
SESSION_DAYS = 7
WAITING = {"AWAITING_REPLY", "TIMES_PROPOSED", "INFO_SENT", "CLARIFICATION_REQUESTED", "OUT_OF_OFFICE"}


# ------------------------------------------------------------------------------------------------ auth
def _sign(ts: str) -> str:
    key = (S.SESSION_SECRET or S.APP_PASSWORD).encode()
    return hmac.new(key, f"ok:{ts}".encode(), hashlib.sha256).hexdigest()


def _valid_session(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    ts, sig = cookie.split(".", 1)
    try:
        fresh = time.time() - int(ts) < SESSION_DAYS * 86400
    except ValueError:
        return False
    return fresh and hmac.compare_digest(sig, _sign(ts))


OPEN_PATHS = ("/login", "/healthz", "/tasks/cron")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not S.APP_PASSWORD:
        if path == "/healthz":
            return await call_next(request)
        return HTMLResponse("<h1>APP_PASSWORD is not set</h1><p>Set APP_PASSWORD (and SESSION_SECRET) in the "
                            "environment. The app refuses to run without a login because it can send email and "
                            "spend API credits.</p>", status_code=503)
    if path.startswith(OPEN_PATHS) or _valid_session(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.method == "GET":
        return RedirectResponse(f"/login?next={path}", status_code=303)
    return JSONResponse({"error": "login required"}, status_code=401)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": error})


@app.post("/login")
def login(password: str = Form(...), next: str = Form("/")):
    if not hmac.compare_digest(password.encode(), S.APP_PASSWORD.encode()):
        time.sleep(1)  # slow down guessing
        return RedirectResponse(f"/login?error=1&next={next}", status_code=303)
    ts = str(int(time.time()))
    resp = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
    resp.set_cookie(COOKIE, f"{ts}.{_sign(ts)}", max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=S.ON_VERCEL)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True, "db": "postgres" if db.using_pg() else "sqlite"}


# ------------------------------------------------------------------------------------------------ helpers
def page(request: Request, name: str, **ctx):
    ctx.setdefault("nav", name.split(".")[0])
    ctx["demo_email"] = S.DEMO_EMAIL or "(not set)"
    ctx["rubric"] = S.RUBRIC_VERSION
    ctx["model"] = S.GEMINI_MODEL
    return templates.TemplateResponse(request, name, ctx)


def jl(s):
    try:
        return json.loads(s) if isinstance(s, str) else (s or {})
    except json.JSONDecodeError:
        return {}


def ist(iso: str | None, fmt: str = "%a %d %b %Y, %H:%M") -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(IST).strftime(fmt) + " IST"
    except ValueError:
        return iso


templates.env.filters["ist"] = ist
templates.env.filters["pretty"] = lambda v: json.dumps(v, indent=1, default=str, ensure_ascii=False)


# ------------------------------------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
def companies(request: Request):
    rows = db.query("SELECT domain, status, total, rounds, pages_fetched, contact_name, contact_title, email_status,"
                    " top_reason, updated_at FROM companies ORDER BY updated_at DESC")
    return page(request, "companies.html", rows=rows)


@app.get("/company/{domain}", response_class=HTMLResponse)
def company(request: Request, domain: str):
    rec = db.query_one("SELECT * FROM companies WHERE domain=?", (domain,))
    if not rec:
        return HTMLResponse("Not found", status_code=404)
    log = jl(rec["decision_log"])
    return page(request, "company.html", nav="companies", rec=rec, log=log, q=log.get("qualification") or {},
                thresholds={"min_total": S.QUALIFY_MIN_TOTAL})


@app.get("/run", response_class=HTMLResponse)
def run_page(request: Request, queued: int = 0):
    default = (S.ROOT / "domains.txt").read_text(encoding="utf-8") if (S.ROOT / "domains.txt").exists() else ""
    hb = db.kv_get("worker_heartbeat")
    hb_age = None
    if hb:
        hb_age = int((datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds())
    rows = jobs.recent()
    active = any(r["status"] in (jobs.QUEUED, jobs.RUNNING) for r in rows)
    return page(request, "run.html", default=default, rows=rows, active=active, queued=queued, hb_age=hb_age)


@app.post("/run")
def run_submit(domains: str = Form(""), mock_hunter: str | None = Form(None), dry_run: str | None = Form(None),
               force: str | None = Form(None)):
    n = jobs.enqueue(domains.splitlines(), mock_hunter=bool(mock_hunter), dry_run=bool(dry_run), force=bool(force))
    return RedirectResponse(f"/run?queued={n}", status_code=303)


@app.get("/threads", response_class=HTMLResponse)
def threads(request: Request):
    rows = db.query("SELECT * FROM threads ORDER BY created_at DESC")
    return page(request, "threads.html", rows=rows)


@app.get("/threads/{tid}", response_class=HTMLResponse)
def thread(request: Request, tid: str, error: str = ""):
    from agent.calendar_provider import fmt_slot
    from agent.graph import get_state
    t = db.query_one("SELECT * FROM threads WHERE thread_id=?", (tid,))
    if not t:
        return HTMLResponse("Not found", status_code=404)
    msgs = db.query("SELECT * FROM messages WHERE thread_id=? ORDER BY seq", (tid,))
    stt = get_state(tid) or {}
    left = None
    if stt.get("reply_deadline") and stt.get("reply_status") in WAITING:
        left = (datetime.fromisoformat(stt["reply_deadline"]) - datetime.now(timezone.utc)).total_seconds() / 3600
    return page(request, "thread.html", nav="threads", t=t, msgs=msgs, s=stt, error=error, left=left,
                proposed=[fmt_slot(x) for x in stt.get("times_proposed", [])],
                latest=[fmt_slot(x) for x in stt.get("last_proposed", [])],
                rejected=[fmt_slot(x) for x in stt.get("rejected_slots", [])],
                confirmed=fmt_slot(stt["confirmed_slot"]) if stt.get("confirmed_slot") else None)


@app.post("/threads/{tid}/reply")
def thread_reply(tid: str, body: str = Form(...)):
    from agent.graph import process_inbound
    if not body.strip():
        return RedirectResponse(f"/threads/{tid}", status_code=303)
    try:
        process_inbound(tid, body.strip(), simulated=True)
    except Exception as e:
        return RedirectResponse(f"/threads/{tid}?error={type(e).__name__}: {str(e)[:200]}", status_code=303)
    return RedirectResponse(f"/threads/{tid}#latest", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, week: str | None = None):
    from agent.calendar_provider import get_calendar
    get_calendar()  # seeds demo busy blocks if empty
    today = datetime.now(IST).date()
    monday = today - timedelta(days=today.weekday())
    start_day = date.fromisoformat(week) if week else monday
    days = [start_day + timedelta(days=i) for i in range(5)]
    start = datetime.combine(start_day, dtime(0), IST)
    ev = db.query("SELECT * FROM calendar_events WHERE status!='cancelled' AND start_utc < ? AND end_utc > ?",
                  ((start + timedelta(days=7)).astimezone(timezone.utc).isoformat(), start.astimezone(timezone.utc).isoformat()))
    grid: dict = {}
    for e in ev:
        s = datetime.fromisoformat(e["start_utc"]).astimezone(IST)
        en = datetime.fromisoformat(e["end_utc"]).astimezone(IST)
        cur = s
        while cur < en:
            grid[(cur.date().isoformat(), cur.strftime("%H:%M"))] = {"kind": e["kind"], "title": e["title"] or ""}
            cur += timedelta(minutes=30)
    times = [f"{h:02d}:{m:02d}" for h in range(S.WORKDAY_START_H, S.WORKDAY_END_H) for m in (0, 30)]
    weeks = [monday + timedelta(weeks=i) for i in range(5)]
    meetings = db.query("SELECT * FROM calendar_events WHERE kind='meeting' ORDER BY start_utc")
    return page(request, "calendar.html", days=days, times=times, grid=grid, weeks=weeks, cur=start_day,
                meetings=meetings)


@app.get("/activity", response_class=HTMLResponse)
def activity(request: Request, ran: int = 0):
    from agent.graph import threads_with_unlogged_entries
    from agent.maintenance import last_maintenance
    rows = db.query("SELECT ts, domain, event, detail FROM activity_log ORDER BY id DESC LIMIT 300")
    unc = db.query("SELECT op_id, domain, kind, status, attempts, detail, updated_at FROM operations "
                   "WHERE status IN ('SEND_UNCERTAIN','PENDING') ORDER BY updated_at DESC")
    return page(request, "activity.html", rows=rows, unc=unc, last=last_maintenance(), ran=ran,
                unlogged=threads_with_unlogged_entries(), spooled=db.spool_count())


@app.post("/tasks/maintenance")
def maintenance_now(request: Request, force: int = 0):
    from agent.maintenance import maybe_run_maintenance, run_maintenance
    res = run_maintenance() if force else maybe_run_maintenance()
    if request.headers.get("accept", "").startswith("text/html"):
        return RedirectResponse("/activity?ran=1", status_code=303)
    return {"ran": res is not None, "result": res}


@app.get("/tasks/cron")
def cron(request: Request):
    """Vercel Cron: authorised by CRON_SECRET (Vercel sends it as a Bearer token)."""
    if not S.CRON_SECRET or request.headers.get("authorization") != f"Bearer {S.CRON_SECRET}":
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    from agent.maintenance import run_maintenance
    return {"ran": True, "result": run_maintenance()}
