"""Web dashboard (FastAPI). Runs on Vercel (api/index.py) and locally: `uvicorn sprinto_web.app:app --reload`.

Same data as the Streamlit dashboard, read from the shared database (Neon). The reply agent and calendar run here
live; long research runs are queued for the worker (`python run.py --worker`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
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
    if S.PUBLIC_ACCESS:  # open demo: no login (cron stays protected by CRON_SECRET)
        return await call_next(request)
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
    return {"ok": True, "db": "postgres" if db.using_pg() else "sqlite", "public": S.PUBLIC_ACCESS}


# ------------------------------------------------------------------------------------------------ helpers
def page(request: Request, name: str, **ctx):
    ctx.setdefault("nav", name.split(".")[0])
    ctx["demo_email"] = S.DEMO_EMAIL or "(not set)"
    ctx["rubric"] = S.RUBRIC_VERSION
    ctx["model"] = S.GEMINI_MODEL
    ctx["public"] = S.PUBLIC_ACCESS
    try:
        ctx["drafts_count"] = len(pending_drafts()) + db.query_one(
            "SELECT count(*) AS n FROM threads WHERE status=?", ("REPLY_DRAFT_READY",))["n"]
    except Exception:
        ctx["drafts_count"] = 0
    return templates.TemplateResponse(request, name, ctx)


def pending_drafts() -> list[dict]:
    out = []
    for r in db.query("SELECT domain, status, decision_log, updated_at FROM companies WHERE status IN (?, ?) "
                      "ORDER BY updated_at DESC", ("DRAFT_READY", "EMAIL_QA_FAILED")):
        log = jl(r["decision_log"])
        d = log.get("draft") or {}
        if d.get("state") == "pending_review":
            c = (log.get("contact") or {}).get("selected") or {}
            out.append({"domain": r["domain"], "status": r["status"], "subject": d.get("subject"), "qa_total": d.get("qa_total"),
                        "qa_passed": d.get("qa_passed"), "edited": d.get("edited"), "name": c.get("name"),
                        "title": c.get("title"), "updated_at": r["updated_at"]})
    return out


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(v) -> str:
    """Hide email addresses (the demo inbox, prospects' research-only emails) on rendered pages."""
    if v is None:
        return ""
    text = v if isinstance(v, str) else json.dumps(v, indent=1, default=str, ensure_ascii=False)
    return EMAIL_RE.sub("[email hidden]", text)


def rate_ok(action: str, limit: int, n: int = 1) -> bool:
    """Simple per-hour counter in the shared database; protects credits if the open link is passed around."""
    key = f"rl:{action}:{datetime.now(timezone.utc):%Y%m%d%H}"
    used = int(db.kv_get(key) or 0)
    if used + n > limit:
        return False
    db.kv_set(key, str(used + n))
    return True


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

STATUS = {  # status -> (style, icon, label). solid = done/positive, tint = needs you, outline = in progress, dash = stopped/negative
    "EMAIL_SENT": ("solid", "✓", "Email sent"), "DRY_RUN_SENT": ("solid", "✓", "Sent (dry run)"),
    "MEETING_BOOKED": ("solid", "✓", "Meeting booked"), "QUALIFIED": ("solid", "✓", "Qualified"), "done": ("solid", "✓", "Done"),
    "DRAFT_READY": ("tint", "✎", "Draft to review"), "REPLY_DRAFT_READY": ("tint", "↩", "Reply to review"),
    "EMAIL_QA_FAILED": ("tint", "✎", "Draft (failed QA)"),
    "AWAITING_REPLY": ("", "◷", "Awaiting reply"), "TIMES_PROPOSED": ("", "◷", "Times proposed"), "INFO_SENT": ("", "◷", "Info sent"),
    "CLARIFICATION_REQUESTED": ("", "◷", "Clarifying"), "OUT_OF_OFFICE": ("", "◷", "Out of office"),
    "queued": ("", "◷", "Queued"), "running": ("", "▸", "Running"),
    "LOW_PRIORITY": ("", "↓", "Low priority"), "NO_SUITABLE_CONTACT": ("", "∅", "No suitable contact"),
    "INSUFFICIENT_EVIDENCE": ("", "?", "Insufficient evidence"), "INSUFFICIENT_PAIN_EVIDENCE": ("", "?", "Pain evidence thin"),
    "DISQUALIFIED": ("dash", "✕", "Disqualified"), "RETRIEVAL_FAILED": ("dash", "✕", "Site unreadable"),
    "ESCALATED": ("dash", "!", "Handed to human"), "BOOKING_UNCERTAIN": ("dash", "!", "Booking unclear"),
    "SEND_UNCERTAIN": ("dash", "?", "Send unconfirmed"), "REJECTED": ("dash", "✕", "Not interested"),
    "OPTED_OUT": ("dash", "✕", "Opted out"), "DRAFT_DISCARDED": ("dash", "✕", "Draft discarded"), "ERROR": ("dash", "!", "Error"),
    "failed": ("dash", "!", "Failed"), "SKIPPED": ("", "↷", "Skipped — already done"),
}


def status_pill(status):
    from markupsafe import Markup, escape
    if not status:
        return ""
    style, icon, label = STATUS.get(status, ("", "•", str(status).replace("_", " ").capitalize()))
    return Markup(f'<span class="pill {style}" title="{escape(status)}"><span class="i">{icon}</span>{escape(label)}</span>')


EVENT_LABELS = {"QUALIFICATION": "Qualification scored", "DRAFT_CREATED": "Cold email drafted", "DRAFT_EDITED": "Draft edited",
                "EMAIL_APPROVED_SENT": "Email approved & sent", "EMAIL_SENT": "Email sent", "THREAD_OPENED": "Thread opened",
                "REPLY_CLASSIFIED": "Prospect reply classified", "REPLY_DRAFTED": "Agent reply drafted",
                "REPLY_DRAFT_APPROVED": "Agent reply approved", "REPLY_SENT": "Reply sent", "TIMES_PROPOSED": "Times proposed",
                "MEETING_CONFIRMED": "Meeting booked", "MEETING_RESCHEDULED": "Meeting moved", "ESCALATED": "Handed to a human",
                "CALENDAR_CHECKED": "Calendar checked", "BOOKING_ATTEMPT": "Booking checked", "INFO_ANSWERED": "Question answered"}
templates.env.filters["status"] = status_pill
templates.env.filters["event"] = lambda e: EVENT_LABELS.get(e, (e or "").replace("_", " ").capitalize())
templates.env.filters["pretty"] = lambda v: redact(json.dumps(v, indent=1, default=str, ensure_ascii=False))
templates.env.filters["redact"] = redact


# ------------------------------------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    return page(request, "overview.html", nav="overview", o=overview_data())


@app.get("/companies", response_class=HTMLResponse)
def companies(request: Request):
    rows = db.query("SELECT domain, status, total, rounds, pages_fetched, contact_name, contact_title, email_status,"
                    " top_reason, updated_at FROM companies ORDER BY COALESCE(total, -1) DESC, domain")
    return page(request, "companies.html", rows=rows)


def overview_data() -> dict:
    """Everything the Overview needs, computed from the shared database in one pass."""
    from agent.graph import get_state
    comps = db.query("SELECT domain, status, total, contact_name, contact_title, decision_log, updated_at FROM companies")
    logs = {c["domain"]: jl(c["decision_log"]) for c in comps}
    researched = len(comps)
    meetings = db.query("SELECT * FROM calendar_events WHERE kind='meeting' AND status!='cancelled' ORDER BY start_utc")
    threads = {t["thread_id"] for t in db.query("SELECT thread_id FROM threads")}
    replied = {r["thread_id"] for r in db.query("SELECT DISTINCT thread_id FROM messages WHERE direction='inbound'")}
    booked_domains = {m["domain"] for m in meetings}

    # Furthest stage each company reached (so the funnel can never widen, even if a later
    # re-run overwrote an earlier record — e.g. a mock run that was followed by a live one).
    def reached(domain: str, l: dict) -> int:
        stage = 0
        if (l.get("qualification") or {}).get("status") == "QUALIFIED" or l.get("status") == "QUALIFIED":
            stage = 1
        if (l.get("contact") or {}).get("status") == "CONTACT_SELECTED":
            stage = 2
        if l.get("draft") or (l.get("email") or {}).get("versions"):
            stage = 3
        if domain in threads or l.get("status") in ("EMAIL_SENT", "DRY_RUN_SENT"):
            stage = 4
        if domain in replied:
            stage = 5
        if domain in booked_domains:
            stage = 6
        return stage
    stage_of = {d: reached(d, l) for d, l in logs.items()}
    names = ["Qualified", "Contact found", "Email drafted", "Email sent", "Prospect replied", "Meeting booked"]
    funnel = [("Researched", researched)] + [(n, sum(1 for s in stage_of.values() if s >= i + 1)) for i, n in enumerate(names)]
    qualified = [d for d, s in stage_of.items() if s >= 1]
    sent = [d for d, s in stage_of.items() if s >= 4]
    top = max(researched, 1)

    # scores vs the 65-point bar
    scores = []
    for c in comps:
        l = logs[c["domain"]]
        total = c["total"] if c["total"] is not None else (l.get("qualification") or {}).get("total")
        scores.append({"domain": c["domain"], "total": total, "status": c["status"],
                       "qualified": c["domain"] in qualified})
    scores.sort(key=lambda r: (-(r["total"] if r["total"] is not None else -1), r["domain"]))

    # what needs a person now
    attention = []
    cold = pending_drafts()
    if cold:
        attention.append({"icon": "✎", "solid": True, "title": f"{len(cold)} cold email{'s' if len(cold) != 1 else ''} to review",
                          "detail": ", ".join(d["domain"] for d in cold[:4]), "href": "/drafts"})
    reply_threads = db.query("SELECT thread_id, company FROM threads WHERE status=?", ("REPLY_DRAFT_READY",))
    if reply_threads:
        attention.append({"icon": "↩", "solid": True, "title": f"{len(reply_threads)} agent repl{'ies' if len(reply_threads) != 1 else 'y'} to review",
                          "detail": ", ".join(t["company"] for t in reply_threads[:4]), "href": "/drafts"})
    for t in db.query("SELECT thread_id, company FROM threads WHERE status IN ('ESCALATED','BOOKING_UNCERTAIN')"):
        st_ = get_state(t["thread_id"]) or {}
        attention.append({"icon": "!", "solid": False, "title": f"{t['company']} handed to a human",
                          "detail": (st_.get("escalation_reason") or "")[:110], "href": f"/threads/{t['thread_id']}"})
    unc = db.query("SELECT domain FROM operations WHERE status='SEND_UNCERTAIN'")
    if unc:
        attention.append({"icon": "?", "solid": False, "title": f"{len(unc)} send{'s' if len(unc) != 1 else ''} not yet confirmed",
                          "detail": "Checked against Resend automatically", "href": "/activity"})
    now = datetime.now(timezone.utc)
    for t in db.query("SELECT thread_id, company, reply_deadline, status FROM threads WHERE status IN "
                      "('AWAITING_REPLY','TIMES_PROPOSED','INFO_SENT','CLARIFICATION_REQUESTED')"):
        if t["reply_deadline"]:
            left = (datetime.fromisoformat(t["reply_deadline"]) - now).total_seconds() / 3600
            if 0 < left <= 24:
                attention.append({"icon": "◷", "solid": False, "title": f"{t['company']}: no reply yet",
                                  "detail": f"Escalates in {left:.0f} h if the prospect stays quiet", "href": f"/threads/{t['thread_id']}"})
    queued = db.query_one("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"]
    hb = db.kv_get("worker_heartbeat")
    hb_age = int((now - datetime.fromisoformat(hb)).total_seconds()) if hb else None
    worker_on = hb_age is not None and hb_age < 90
    if queued and not worker_on:
        attention.append({"icon": "■", "solid": False, "title": f"{queued} run{'s' if queued != 1 else ''} waiting for the worker",
                          "detail": "Start python run.py --worker on the laptop", "href": "/run"})

    upcoming = [m for m in meetings if datetime.fromisoformat(m["end_utc"]) > now][:4]
    for m in upcoming:
        s = datetime.fromisoformat(m["start_utc"]).astimezone(IST)
        m["mon"], m["day"], m["when"] = s.strftime("%b").upper(), s.strftime("%d"), s.strftime("%a, %H:%M IST")
        t = db.query_one("SELECT company, contact_name FROM threads WHERE thread_id=?", (m["domain"],)) or {}
        m["company"], m["who"] = t.get("company") or m["domain"], t.get("contact_name") or ""

    recent = db.query("SELECT ts, domain, event FROM activity_log ORDER BY id DESC LIMIT 7")
    last_run = db.query_one("SELECT max(updated_at) AS t FROM companies")["t"]
    q_rate = round(100 * len(qualified) / researched) if researched else 0
    return {"tiles": {"researched": researched, "qualified": len(qualified), "q_rate": q_rate, "sent": len(sent),
                      "meetings": len(booked_domains), "review": len(cold) + len(reply_threads), "replied": sum(1 for s in stage_of.values() if s >= 5)},
            "funnel": [{"name": n, "value": v, "pct": round(100 * v / top)} for n, v in funnel],
            "scores": scores, "threshold": S.QUALIFY_MIN_TOTAL, "attention": attention, "upcoming": upcoming,
            "recent": recent, "worker_on": worker_on, "hb_age": hb_age, "queued": queued, "last_run": last_run}


@app.get("/company/{domain}", response_class=HTMLResponse)
def company(request: Request, domain: str):
    rec = db.query_one("SELECT * FROM companies WHERE domain=?", (domain,))
    if not rec:
        return HTMLResponse("Not found", status_code=404)
    log = jl(rec["decision_log"])
    from outreach.records import draft_checks
    d = log.get("draft") or {}
    person = (log.get("contact") or {}).get("selected") or {}
    checks = draft_checks(d.get("subject", ""), d.get("body", ""), person.get("first_name", "")) if d else []
    return page(request, "company.html", nav="companies", rec=rec, log=log, q=log.get("qualification") or {},
                thresholds={"min_total": S.QUALIFY_MIN_TOTAL}, draft=d, person=person, checks=checks,
                msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""))


def pending_reply_drafts() -> list[dict]:
    from agent.graph import get_state
    out = []
    for t in db.query("SELECT * FROM threads WHERE status=? ORDER BY created_at DESC", ("REPLY_DRAFT_READY",)):
        for d in (get_state(t["thread_id"]) or {}).get("pending_drafts") or []:
            out.append({"thread_id": t["thread_id"], "company": t["company"], "name": t["contact_name"],
                        "kind": d["kind"], "created_at": d.get("created_at"), "subject": d.get("subject")})
    return out


@app.get("/drafts", response_class=HTMLResponse)
def drafts(request: Request):
    return page(request, "drafts.html", rows=pending_drafts(), replies=pending_reply_drafts())


@app.post("/company/{domain}/draft")
def draft_action(domain: str, action: str = Form(...), subject: str = Form(""), body: str = Form("")):
    from urllib.parse import quote
    from outreach import records
    back = f"/company/{domain}#draft"
    try:
        if action == "save":
            records.save_draft(domain, subject, body)
            return RedirectResponse(f"/company/{domain}?msg=Draft saved#draft", status_code=303)
        if action == "discard":
            records.discard_draft(domain)
            return RedirectResponse(f"/company/{domain}?msg=Draft discarded#draft", status_code=303)
        if action == "send":
            if not rate_ok("sends", S.RATE_LIMIT_SENDS_PER_HOUR):
                return RedirectResponse(f"/company/{domain}?err={quote('Rate limit reached for sends; try later')}#draft", status_code=303)
            res = records.send_draft(domain, subject, body)
            if res["status"] in ("SENT", "DUPLICATE_BLOCKED"):
                return RedirectResponse(f"/threads/{domain}", status_code=303)
            return RedirectResponse(f"/company/{domain}?err={quote('Send ' + res['status'] + ': ' + str(res.get('error') or ''))}#draft",
                                    status_code=303)
    except (ValueError, KeyError) as e:
        return RedirectResponse(f"/company/{domain}?err={quote(str(e))}#draft", status_code=303)
    return RedirectResponse(back, status_code=303)


@app.get("/run", response_class=HTMLResponse)
def run_page(request: Request, queued: int = 0, limited: int = 0):
    default = (S.ROOT / "domains.txt").read_text(encoding="utf-8") if (S.ROOT / "domains.txt").exists() else ""
    hb = db.kv_get("worker_heartbeat")
    hb_age = None
    if hb:
        hb_age = int((datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds())
    rows = jobs.recent()
    active = any(r["status"] in (jobs.QUEUED, jobs.RUNNING) for r in rows)
    return page(request, "run.html", default=default, rows=rows, active=active, queued=queued, hb_age=hb_age,
                limited=limited, limit=S.RATE_LIMIT_DOMAINS_PER_HOUR)


@app.post("/run")
def run_submit(domains: str = Form(""), mock_hunter: str | None = Form(None), dry_run: str | None = Form(None),
               force: str | None = Form(None)):
    lines = [d for d in domains.splitlines() if d.split("#")[0].strip()]
    if not rate_ok("domains", S.RATE_LIMIT_DOMAINS_PER_HOUR, len(lines)):
        return RedirectResponse("/run?limited=1", status_code=303)
    n = jobs.enqueue(lines, mock_hunter=bool(mock_hunter), dry_run=bool(dry_run), force=bool(force))
    return RedirectResponse(f"/run?queued={n}", status_code=303)


@app.get("/threads", response_class=HTMLResponse)
def threads(request: Request):
    rows = db.query("SELECT * FROM threads ORDER BY created_at DESC")
    return page(request, "threads.html", rows=rows)


@app.get("/threads/{tid}", response_class=HTMLResponse)
def thread(request: Request, tid: str, error: str = "", msg: str = ""):
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
    pending = [{**d, "slot_text": fmt_slot(d["slot"]) if d.get("slot") else None,
                "slots_text": [fmt_slot(x) for x in d.get("slots", [])]} for d in stt.get("pending_drafts") or []]
    return page(request, "thread.html", nav="threads", t=t, msgs=msgs, s=stt, error=error, msg=msg, left=left, pending=pending,
                proposed=[fmt_slot(x) for x in stt.get("times_proposed", [])],
                latest=[fmt_slot(x) for x in stt.get("last_proposed", [])],
                rejected=[fmt_slot(x) for x in stt.get("rejected_slots", [])],
                confirmed=fmt_slot(stt["confirmed_slot"]) if stt.get("confirmed_slot") else None)


@app.post("/threads/{tid}/draft/{draft_id}")
def thread_draft_action(tid: str, draft_id: str, action: str = Form(...), subject: str = Form(""), body: str = Form("")):
    """Reviewer approves (optionally edited) or discards an agent reply draft."""
    from urllib.parse import quote
    from agent.graph import approve_reply, discard_reply
    try:
        if action == "discard":
            discard_reply(tid, draft_id)
            return RedirectResponse(f"/threads/{tid}?msg={quote('Reply discarded; thread handed to a human')}#latest", status_code=303)
        if not rate_ok("sends", S.RATE_LIMIT_SENDS_PER_HOUR):
            return RedirectResponse(f"/threads/{tid}?error={quote('Rate limit reached for sends; try later')}", status_code=303)
        s = approve_reply(tid, draft_id, subject or None, body or None)
        if s.get("skip_reason"):
            return RedirectResponse(f"/threads/{tid}?error={quote(s['skip_reason'])}", status_code=303)
        note = "Sent" if s.get("reply_status") != "REPLY_DRAFT_READY" else "Not sent: the slot was taken; review the new draft with alternative times"
        return RedirectResponse(f"/threads/{tid}?msg={quote(note)}#latest", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/threads/{tid}?error={quote(type(e).__name__ + ': ' + str(e)[:200])}", status_code=303)


@app.post("/threads/{tid}/reply")
def thread_reply(tid: str, body: str = Form(...)):
    from agent.graph import process_inbound
    if not body.strip():
        return RedirectResponse(f"/threads/{tid}", status_code=303)
    if not rate_ok("replies", S.RATE_LIMIT_REPLIES_PER_HOUR):
        return RedirectResponse(f"/threads/{tid}?error=Rate limit reached ({S.RATE_LIMIT_REPLIES_PER_HOUR} simulated "
                                f"replies per hour). Try again later.", status_code=303)
    try:
        process_inbound(tid, body.strip(), simulated=True)
    except Exception as e:
        return RedirectResponse(f"/threads/{tid}?error={type(e).__name__}: {str(e)[:200]}", status_code=303)
    return RedirectResponse(f"/threads/{tid}?msg=The%20agent%20drafted%20a%20reply%20%E2%80%94%20review%20it%20below", status_code=303)


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
