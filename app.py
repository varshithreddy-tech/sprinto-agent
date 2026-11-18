"""Streamlit dashboard — streamlit run app.py"""
from __future__ import annotations

import json
import warnings
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

warnings.filterwarnings("ignore")

from config import settings as S  # noqa: E402
from storage import db  # noqa: E402

st.set_page_config(page_title="Sprinto Outbound Agent (demo)", layout="wide")
IST = ZoneInfo(S.ORGANIZER_TZ)


def jl(s):
    try:
        return json.loads(s) if isinstance(s, str) else (s or {})
    except json.JSONDecodeError:
        return {}


@st.cache_resource
def _deadline_watcher():
    """One background thread per dashboard process: checks no-reply deadlines every DEADLINE_CHECK_MINUTES."""
    from agent.graph import start_deadline_watcher
    return start_deadline_watcher()


_deadline_watcher()

page = st.sidebar.radio("Page", ["Run", "Companies", "Threads", "Calendar", "Activity log"])
st.sidebar.caption(f"Rubric {S.RUBRIC_VERSION} · model {S.GEMINI_MODEL}")
st.sidebar.caption(f"All email goes to DEMO_EMAIL only: {S.DEMO_EMAIL or '(not set)'}")
missing = S.missing_keys("GEMINI_API_KEY", "HUNTER_API_KEY", "RESEND_API_KEY", "DEMO_EMAIL")
if missing:
    st.sidebar.warning("Missing in .env: " + ", ".join(missing))

# ============================================================================ RUN
if page == "Run":
    st.title("Run outbound pipeline")
    default = open(S.ROOT / "domains.txt", encoding="utf-8").read() if (S.ROOT / "domains.txt").exists() else ""
    domains_txt = st.text_area("Domains (one per line, # comments allowed)", default, height=220)
    c1, c2, c3 = st.columns(3)
    # Safe defaults: a real Hunter lookup or real email needs a deliberate switch-off.
    mock = c1.toggle("Mock Hunter (fixtures)", value=True, help="Off = live Hunter lookups (uses credits)")
    dry = c2.toggle("Dry run (no real send)", value=True, help="Off = real emails to DEMO_EMAIL via Resend")
    force = c3.toggle("Re-process already-sent domains", value=False)
    if not (mock and dry):
        st.warning("LIVE mode: " + ", ".join(x for x, on in [("real Hunter lookups", not mock), ("real emails to DEMO_EMAIL", not dry)] if on))
    if st.button("Run", type="primary"):
        need = S.missing_keys("GEMINI_API_KEY") + ([] if mock else S.missing_keys("HUNTER_API_KEY")) + \
            ([] if dry else S.missing_keys("RESEND_API_KEY", "DEMO_EMAIL"))
        if need:
            st.error(f"Missing in .env: {', '.join(need)}")
        else:
            if dry and not S.DEMO_EMAIL:
                S.DEMO_EMAIL = "demo@example.invalid"
            from pipeline import run_batch
            doms = [d.split("#")[0].strip() for d in domains_txt.splitlines() if d.split("#")[0].strip()]
            boxes = {d: st.empty() for d in doms}
            for d in doms:
                boxes[d].info(f"**{d}** — queued")

            def progress(d, msg):
                if d in boxes:
                    boxes[d].info(f"**{d}** — {msg}")
            results = run_batch(doms, mock_hunter=mock, dry_run=dry, force=force, progress=progress)
            for r in results:
                status = r.get("status")
                (boxes[r["domain"]].success if status in ("EMAIL_SENT", "DRY_RUN_SENT", "QUALIFIED") else boxes[r["domain"]].warning)(
                    f"**{r['domain']}** — {status} · total {(r.get('qualification') or {}).get('total', r.get('total', '–'))} · {r.get('top_reason') or ''}")
            st.success("Batch finished. See Companies for full decision logs.")

# ============================================================================ COMPANIES
elif page == "Companies":
    st.title("Companies")
    rows = db.query("SELECT domain, status, total, rounds, pages_fetched, contact_name, contact_title, email_status, top_reason, updated_at FROM companies ORDER BY updated_at DESC")
    if not rows:
        st.info("No companies yet — run the pipeline first.")
        st.stop()
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    dom = st.selectbox("Open decision log", [r["domain"] for r in rows])
    rec = db.query_one("SELECT * FROM companies WHERE domain=?", (dom,))
    log = jl(rec["decision_log"])
    st.header(f"{dom} — {log.get('status')}")
    st.caption(f"Rubric {log.get('rubric_version')} · mode {log.get('mode')} · run {log.get('run_id')}")
    if log.get("top_reason"):
        st.write("**Top reason:**", log["top_reason"])
    if log.get("escalation_reason"):
        st.error(f"Escalation: {log['escalation_reason']}")

    q = log.get("qualification") or {}
    if q:
        st.subheader(f"Qualification: {q.get('total')} / 100 (best case with gaps filled: {q.get('max_possible')})")
        st.caption(f"Thresholds — total ≥ {S.QUALIFY_MIN_TOTAL}, business fit = 2, compliance workload ≥ 1. Points = weight × level ÷ 2.")
        st.dataframe(pd.DataFrame([{"dimension": n, "weight": d["weight"], "level": d["level"], "LLM level": d["llm_level"],
                                    "points": d["points"], "zero reason": d["zero_reason"], "evidence": ", ".join(d["evidence_ids"]),
                                    "reason": d["reason"], "code adjustments": "; ".join(d["adjustments"])}
                                   for n, d in q["dimensions"].items()]), width="stretch", hide_index=True)
        st.write("**Flags:**", q.get("flags"))

    st.subheader("Research rounds (gap → source → new evidence → decision)")
    for r in log.get("rounds", []):
        with st.expander(f"Round {r['round']}: {r.get('decision')} {'· total ' + str(r.get('total')) if r.get('total') is not None else ''}"):
            st.write("**Evidence gap:**", r.get("evidence_gap"))
            st.write("**Selected sources:**")
            st.dataframe(pd.DataFrame(r.get("selected_sources", [])), width="stretch", hide_index=True)
            st.write("**New evidence:**", ", ".join(r.get("new_evidence", [])) or "none")
            if r.get("reasons"):
                st.write("**Decision reasons:**", "; ".join(r["reasons"]))

    facts = log.get("facts", [])
    if facts:
        st.subheader(f"Evidence ({sum(f['verified'] for f in facts)} verified / {len(facts)})")
        st.dataframe(pd.DataFrame([{"id": f["id"], "field": f["field"], "value": f["value"], "label": f["label"],
                                    "verified": f["verified"], "source": f["source_url"], "exact excerpt": f["exact_excerpt"],
                                    "retrieved": f.get("retrieval_date"), "published": f.get("publication_date"),
                                    "note": f.get("verification_note")} for f in facts]),
                     width="stretch", hide_index=True,
                     column_config={"source": st.column_config.LinkColumn("source")})
    with st.expander(f"Pages ({len(log.get('pages', []))})"):
        st.dataframe(pd.DataFrame(log.get("pages", [])), width="stretch", hide_index=True)

    c = log.get("contact")
    if c:
        st.subheader(f"Contact selection — {c.get('status')}")
        st.caption(c.get("selection_reason", ""))
        st.dataframe(pd.DataFrame([{"selected": x.get("selected", False), "name": x["name"], "title": x["title"],
                                    "role category": x["role_category"], **x["scores"], "total": x["total"], "passed": x["passed"],
                                    "first-party page": x.get("first_party_page"), "adjustments": "; ".join(x.get("adjustments", [])),
                                    "reasons": " | ".join(f"{k}: {v}" for k, v in x["reasons"].items())}
                                   for x in c["candidates"]]), width="stretch", hide_index=True)
    pains = log.get("pains")
    if pains:
        st.subheader(f"Pain hypotheses — {pains['status']}")
        for i, h in enumerate(pains["hypotheses"], 1):
            with st.expander(f"#{i} · {h['score']}/10 · {'PASS' if h['passed'] else 'FAIL'} · {h['capability']}"):
                st.write("**Observed fact:**", h["observed_fact"], f"({', '.join(h['evidence_ids'])})")
                st.write("**Hypothesis:**", h["hypothesis"])
                st.write("**Role relevance:**", h["role_relevance"])
                st.write("**Possible consequence:**", h["possible_consequence"])
                st.write("**Scores:**", h["scores"], h.get("code_notes") or "", h.get("fail_reasons") or "")
    em = log.get("email")
    if em:
        st.subheader(f"Email — {em['status']}")
        for v in em["versions"]:
            with st.expander(f"Version {v['version']} · QA {v['qa_total']}/10 · {'PASS' if v['passed'] else 'FAIL'} · {v['words']} words",
                             expanded=v["version"] == em.get("final_version")):
                st.write(f"**Subject:** {v['subject']}")
                st.text(v["body"])
                st.write("**QA scores:**", v["qa_scores"])
                st.write("**Mandatory checks:**", v["qa_checks"])
                if v.get("issues") or v.get("code_notes"):
                    st.write("**Issues:**", v.get("issues"), v.get("code_notes"))
    if log.get("send"):
        st.subheader("Send")
        st.json(log["send"])

# ============================================================================ THREADS
elif page == "Threads":
    from agent.graph import check_deadlines, get_state, process_inbound
    st.title("Threads")
    threads = db.query("SELECT * FROM threads ORDER BY created_at DESC")
    from agent.graph import LAST_DEADLINE_CHECK
    c1, c2 = st.columns([1, 3])
    if c1.button("Check no-reply deadlines now"):
        st.write(check_deadlines() or "No thread passed its deadline.")
    last = LAST_DEADLINE_CHECK
    c2.caption(f"Auto-check every {S.DEADLINE_CHECK_MINUTES:g} min while the dashboard runs · last check: "
               f"{last['at'] or 'pending'} · escalated last time: {len(last['escalated'])}"
               + (f" · error: {last['error']}" if last.get("error") else ""))
    if not threads:
        st.info("No sent emails yet.")
        st.stop()
    tid = st.selectbox("Thread", [t["thread_id"] for t in threads],
                       format_func=lambda t: next(f"{x['company']} — {x['contact_name']} ({x['status']})" for x in threads if x["thread_id"] == t))
    t = next(x for x in threads if x["thread_id"] == tid)
    left, right = st.columns([3, 2])
    with left:
        st.subheader(t["subject"])
        for m in db.query("SELECT * FROM messages WHERE thread_id=? ORDER BY created_at, rowid", (tid,)):
            who = "Prospect (simulated)" if m["direction"] == "inbound" else "Agent → DEMO_EMAIL"
            with st.chat_message("user" if m["direction"] == "inbound" else "assistant"):
                st.caption(f"{who} · {m['created_at']} · {m['message_id']}" + (f" · provider id {m['provider_id']}" if m["provider_id"] else ""))
                st.text(m["body"])
        st.markdown("### DEMO — simulated prospect replies")
        st.caption("Resend's free tier can't receive mail. Type a reply as the prospect; it is stored as an inbound message on this thread and the reply agent processes it.")
        with st.form("reply", clear_on_submit=True):
            body = st.text_area("Simulated prospect reply", height=120)
            if st.form_submit_button("Send as prospect") and body.strip():
                with st.spinner("Reply agent working…"):
                    try:
                        process_inbound(tid, body.strip(), simulated=True)
                    except Exception as e:
                        st.error(f"{type(e).__name__}: {e}")
                st.rerun()
    with right:
        stt = get_state(tid)
        st.subheader("Agent state (LangGraph checkpoint)")
        if stt:
            from agent.calendar_provider import fmt_slot
            st.metric("reply_status", stt.get("reply_status"))
            dl = stt.get("reply_deadline")
            if dl and stt.get("reply_status") in ("AWAITING_REPLY", "TIMES_PROPOSED", "INFO_SENT", "CLARIFICATION_REQUESTED", "OUT_OF_OFFICE"):
                left = datetime.fromisoformat(dl) - datetime.now(timezone.utc)
                hrs = left.total_seconds() / 3600
                st.caption(f"No-reply deadline: {datetime.fromisoformat(dl).astimezone(IST):%a %d %b %H:%M} IST · "
                           + (f"{hrs:.1f} h left" if hrs > 0 else "passed — escalates on the next check"))
            st.write({k: stt.get(k) for k in ["reply_round", "meeting_status", "event_id", "confirmed_slot", "requested_slot",
                                               "prospect_tz", "organizer_tz", "calendar_checked", "checked_at", "suppressed",
                                               "escalation_reason", "reply_deadline", "crm_logged", "retry_counts", "referral"]})
            st.write("**Proposed slots:**", [fmt_slot(s) for s in stt.get("times_proposed", [])])
            st.write("**Rejected slots:**", [fmt_slot(s) for s in stt.get("rejected_slots", [])])
            with st.expander("Last classification"):
                st.json(stt.get("classification") or {})
            with st.expander("Processed / agent message ids"):
                st.write(stt.get("processed_message_ids"), stt.get("agent_message_ids"))

# ============================================================================ CALENDAR
elif page == "Calendar":
    from agent.calendar_provider import get_calendar
    get_calendar()  # seeds if empty
    st.title("Demo calendar (organizer: Asia/Kolkata)")
    today = datetime.now(IST).date()
    monday = today - timedelta(days=today.weekday())
    week = st.selectbox("Week", [monday + timedelta(weeks=i) for i in range(5)], format_func=lambda d: f"Week of {d:%a %d %b %Y}")
    days = [week + timedelta(days=i) for i in range(5)]
    start = datetime.combine(week, time(0), IST)
    ev = db.query("SELECT * FROM calendar_events WHERE status!='cancelled' AND start_utc < ? AND end_utc > ?",
                  ((start + timedelta(days=7)).astimezone(timezone.utc).isoformat(), start.astimezone(timezone.utc).isoformat()))
    grid = {}
    for e in ev:
        s = datetime.fromisoformat(e["start_utc"]).astimezone(IST)
        en = datetime.fromisoformat(e["end_utc"]).astimezone(IST)
        cur = s
        while cur < en:
            grid[(cur.date(), cur.strftime("%H:%M"))] = ("📅 " if e["kind"] == "meeting" else "■ ") + (e["title"] or "")
            cur += timedelta(minutes=30)
    times = [f"{h:02d}:{m:02d}" for h in range(S.WORKDAY_START_H, S.WORKDAY_END_H) for m in (0, 30)]
    df = pd.DataFrame({f"{d:%a %d %b}": [grid.get((d, tm), "") for tm in times] for d in days}, index=times)

    def color(v):
        if v.startswith("📅"):
            return "background-color: #2e7d32; color: white"
        if v.startswith("■"):
            return "background-color: #9e9e9e; color: white"
        return ""
    st.dataframe(df.style.map(color), width="stretch", height=720)
    st.caption("■ busy block (seeded) · 📅 booked meeting. Weekdays 09:00–18:00 IST, 30-minute slots, 30-day horizon.")
    meetings = [e for e in db.query("SELECT * FROM calendar_events WHERE kind='meeting' ORDER BY start_utc")]
    if meetings:
        st.subheader("Booked meetings")
        st.dataframe(pd.DataFrame([{"event_id": m["event_id"], "title": m["title"], "domain": m["domain"],
                                    "start (IST)": datetime.fromisoformat(m["start_utc"]).astimezone(IST).strftime("%a %d %b %Y %H:%M"),
                                    "attendee": m["attendee"], "status": m["status"]} for m in meetings]), hide_index=True)

# ============================================================================ ACTIVITY LOG
elif page == "Activity log":
    st.title("DEMO ACTIVITY LOG — not a real CRM")
    rows = db.query("SELECT ts, domain, event, detail FROM activity_log ORDER BY id DESC LIMIT 500")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
