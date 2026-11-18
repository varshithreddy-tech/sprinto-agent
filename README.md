# Sprinto outbound agent (demo)

Paste a list of domains and the agent works end to end: it researches each company's own website, qualifies it against the 100-point rubric, picks the right person from Hunter, writes two evidence-backed pain hypotheses, drafts and QAs a cold email, sends it (to your demo inbox only), then handles replies and books meetings on a demo calendar.

**The principle: code decides, AI judges and writes, every claim cites a source.** Gemini extracts facts with verbatim quotes, proposes rubric levels, and writes text. Python code handles the rest:

- checks that every quote really appears on the page;
- checks date windows;
- computes all scores and thresholds;
- picks the contact;
- enforces every guard.

## Setup

```bash
cd sprinto-agent
uv venv --python 3.12 .venv          # or: python -m venv .venv
.venv\Scripts\activate               # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env               # then fill it in
```

### Keys (`.env`)

| Key | What it is |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio key |
| `GEMINI_MODEL` | defaults to `gemini-3.8-flash` (see "Changes from the brief") |
| `HUNTER_API_KEY` | hunter.io API key (Domain Search) |
| `RESEND_API_KEY` | resend.com API key |
| `DEMO_EMAIL` | **the email address of your Resend account**. Free Resend (no verified domain) sends from `onboarding@resend.dev` only to this address |
| `ORGANIZER_TZ` | `Asia/Kolkata` |
| `REPLY_DEADLINE_HOURS` | `48` |

## How to run

```bash
# Full offline-ish dry run: live website research + Gemini, fixture Hunter data, no real email
python run.py --domains domains.txt --dry-run --mock-hunter

# Live: real Hunter + real Resend send to DEMO_EMAIL
python run.py --domains domains.txt

# Escalate threads whose 48h no-reply deadline passed
python run.py --check-deadlines

# Dashboard
streamlit run app.py

# Tests (scoring, reply agent, failure handling — no API calls)
python -m pytest -q
```

Domains already sent are skipped on re-run (resume after a restart). Add `--force` to process them again; the stable operation ID still blocks duplicate sends.

## How to demo

1. **Run** page: paste domains, choose Mock Hunter and Dry run as needed, then click **Run**. Progress updates live for each domain.
2. **Companies** page: open any row to see its full decision log:
   - evidence table with source links and exact quotes;
   - the 5 dimension levels, LLM level vs code-adjusted level, and points;
   - every research round (gap → source → new evidence → decision);
   - contact scores for every Hunter candidate;
   - both pain hypotheses with scores;
   - every email version with QA scores and mandatory checks;
   - the send ID.
3. **Threads** page: open a sent email. Under **DEMO — simulated prospect replies**, type a reply as the prospect, for example:
   - "Interested, happy to chat" → the agent checks the calendar and proposes 3 slots;
   - "Option 2 works" → the agent re-checks that slot, books it, and emails a confirmation with an `.ics` invite;
   - "None of these, next week afternoon?" → counterproposal round 1. A second counterproposal hands off to a human;
   - After booking, "Something came up, can we move it to Thursday afternoon?" → the booking is kept, new slots are offered, and picking one moves the same event (updated .ics).
   - "What's the pricing?" → handoff. "Unsubscribe" → suppressed. An out-of-office reply is classified separately.

   Agent replies go through Resend to `DEMO_EMAIL` and also appear in the thread. The live LangGraph state shows on the right.
4. **Calendar** page: week view showing seeded busy blocks and booked meetings.
5. **Activity log** page: `DEMO ACTIVITY LOG — not a real CRM`.

## Architecture

```
domains → scraper (robots, httpx → Playwright fallback, discovery, trafilatura cleaning, 7-day cache)
        → extraction (Gemini, JSON → Pydantic; excerpts verified against page text, else downgraded to unknown)
        → qualification (Gemini levels → code: evidence check, 6/12-month windows, weight×level÷2, thresholds, routing)
        → research loop (max 3 rounds × 2 pages, driven by which dimension lacks evidence)
        → Hunter Domain Search (QUALIFIED only, cached per domain) → contact scoring (all candidates, role preference)
        → 2 pain hypotheses (approved capabilities only) → email draft → QA (≤2 revisions)
        → Resend (DEMO_EMAIL only, idempotency key = stable op ID) → reply thread
        → LangGraph reply agent (SQLite checkpoints): classify → calendar → propose / answer / book / stop / suppress / escalate
```

| Folder | Contents |
|---|---|
| `config/` | `settings.py` (every weight, threshold and limit), `prompts/*.txt` (versioned by content hash), `sprinto_capabilities.md`, `rubric_version.txt` |
| `scraper/` | `robots.py`, `fetch.py`, `discover.py`, `clean.py`, `cache.py` |
| `research/` | `extract.py`, `qualify.py`, `loop.py`, `scoring.py` (pure code, unit-tested) |
| `outreach/` | `hunter.py`, `contact.py`, `pains.py`, `email_draft.py`, `email_qa.py`, `send.py`, `capabilities.py` |
| `agent/` | `graph.py` (LangGraph), `tools.py` (4-part tool descriptions), `calendar_provider.py` (`CalendarProvider` interface + `DemoCalendar`), `state.py` |
| `storage/` | `db.py`: SQLite schema and migrations. Holds the page cache, evidence, companies + decision logs, contacts, pains, emails, operations, threads/messages, calendar, activity log and LangGraph checkpoints |
| `data/` | `sprinto.db`, `decision_logs/<domain>.json` (gitignored) |

### Statuses

- **Research:** `QUALIFIED`, `LOW_PRIORITY`, `DISQUALIFIED`, `INSUFFICIENT_EVIDENCE`, `RETRIEVAL_FAILED`
- **Outreach:** `NO_SUITABLE_CONTACT`, `INSUFFICIENT_PAIN_EVIDENCE`, `EMAIL_QA_FAILED`, `EMAIL_SENT` / `DRY_RUN_SENT`, `SEND_UNCERTAIN`
- **Integrations:** `HUNTER_QUOTA_PAUSED`, `HUNTER_SETUP_ERROR`, `HUNTER_PAUSED`
- **Reply agent:** `AWAITING_REPLY`, `RESCHEDULE_REQUESTED` (meeting_status), `TIMES_PROPOSED`, `INFO_SENT`, `CLARIFICATION_REQUESTED`, `MEETING_BOOKED`, `REJECTED`, `OPTED_OUT`, `OUT_OF_OFFICE`, `DELIVERY_NOTICE`, `ESCALATED`, `BOOKING_UNCERTAIN`

### Safety guards

- `outreach/send.py::assert_demo_recipient` raises unless there is exactly one recipient, it equals `DEMO_EMAIL`, and CC/BCC are empty. Every sender class calls it.
- Prospect emails from Hunter are stored as `email_research_only` and are never passed to a sender.
- Every send gets an operation ID that is stored *before* sending. Resend also receives it as an idempotency key, so retrying after a timeout cannot create a second email. If the outcome is still unresolved, the status is `SEND_UNCERTAIN`.
- Agent-generated message IDs are recorded and ignored by the reply agent, which prevents reply loops. Each processed message ID is stored, so replays are not processed twice.
- Page text and email text go to Gemini inside `<page>`/`<message>` tags, with a system instruction to treat them as data.

## Changes from the brief (and why)

1. **Model is `gemini-3.8-flash`, not `gemini-2.5-flash`.** Google now returns 404 "no longer available to new users" for `gemini-2.5-flash` on this key. The model name is still config (`GEMINI_MODEL`).
2. **Short-block rule:** blocks under 8 words are dropped *except* short blocks with compliance or timing signals (for example "SOC 2 Type II", "Series B", dates). Otherwise trust-page badges and job dates would be lost, and that evidence decides two dimensions.
3. **Sprinto capability sources come from docs.sprinto.com.** `sprinto.com` serves a bot challenge to the crawler, and the rules forbid bypassing it. `python -m outreach.capabilities --refresh` records that and uses Sprinto's public docs site instead.
4. **Single-page apps that never render for a headless browser** (for example some hosted trust centers) are recorded as `RETRIEVAL_FAILED`, not as a near-empty page. The loop substitutes the next page of the same question type.
5. **Timing window rule (code):** Level 2 needs an audit initiative, relevant hiring, or a compliance deadline dated within 183 days. Level 1 needs any of those, or an expansion or enterprise launch, dated within 365 days. Undated signals score 0, so a live job post with no visible date scores 0, as the rubric requires.
6. **Counterproposal limit:** the 2nd counterproposal without a confirmation triggers handoff, with `reply_round` = 2.
7. **Mock Hunter fixtures** (`tests/fixtures/hunter/*.json`) contain **fictional** people, clearly labelled. Because their names don't appear on the company's own pages, company association is correctly capped at 1.
8. **Rescheduling:** the reply classifier has a `reschedule` category, used only when a meeting is booked. The existing event is **kept until a new slot is confirmed**, then **updated in place**: same event ID, and the ICS `SEQUENCE` goes up by 1 on every move, so calendar apps update the invite instead of duplicating it. The old slot is marked rejected and never offered again. An exact new slot gets a fresh availability check; a busy one gets alternatives, with the booking untouched. A reschedule starts a fresh negotiation (`reply_round` resets to 0). A "reschedule" request with nothing booked is treated as a normal counterproposal. "Option N" / "the first one" always refers to the latest batch of offered slots (`last_proposed`), even when that batch has fewer than 3.
9. **Splash/storefront pages:** the page `<title>` and meta description are always kept. If dropping short blocks would leave a page under 30 words, the short blocks are kept instead. Without this, bewakoof.com's homepage cleaned to nothing and showed up as a retrieval failure instead of a clear non-SaaS business.
10. **Blocked competitor sites:** drata.com serves a bot challenge (never bypassed). When a homepage can't be retrieved *and* the domain is on the configured competitor list (`COMPETITOR_DOMAINS`, from the rubric's examples), it is routed to `DISQUALIFIED`, and the log states there was no web evidence. Any other blocked domain stays `RETRIEVAL_FAILED`, not poor fit.
11. **Staging/dev subdomains** (`stage.`, `dev.`, `qa.` …) found in sitemaps are excluded from discovery.
