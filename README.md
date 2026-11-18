# Sprinto outbound agent (demo)

Paste a list of domains and the agent works end to end: it researches each company's own website, qualifies it against the 100-point rubric, picks the right person from Hunter, writes two evidence-backed pain hypotheses, drafts and QAs a cold email, sends it (to your demo inbox only), then handles replies and books meetings on a demo calendar.

**The principle: code decides, AI judges and writes, every claim cites a source.** Gemini extracts facts with verbatim quotes, proposes rubric levels, and writes text. Python code handles the rest:

- checks that every quote really appears on the page;
- checks date windows;
- computes all scores and thresholds;
- picks the contact;
- enforces every guard.

## ICP definition and rationale: who counts as a "right company"

> These are this project's own working rules for prospecting fit, derived from the case brief and Sprinto's product. They are **not** Sprinto's official internal ICP. Every weight and threshold lives in `config/settings.py` and is enforced in code (`research/scoring.py`, unit-tested).

### Starting point

Sprinto sells compliance automation (SOC 2, ISO 27001, GDPR) to SaaS companies. The brief describes the buyer as *SaaS, typically Series A–C, preparing for a compliance audit or under pressure from enterprise customers to get certified.* So two conditions have to hold for Sprinto to be relevant:

1. **The company has the problem Sprinto solves.** It runs software that handles customer data, and it has (or will soon have) real compliance work: audits, frameworks, controls, customer security reviews.
2. **Someone outside is asking for proof.** Enterprise or regulated customers want certifications, questionnaires and trust documentation.

A reason to reach out *now* makes a prospect better, but it isn't required.

### Why not filter on funding stage and headcount

"Series A–C" describes the typical buyer, but it's a poor filter for an automated system:

- **It's often not published on the company's own site,** and the agent reads only that site, never LinkedIn or databases.
- **Funding isn't budget, and a stage says nothing about compliance pressure.** A Series B company selling to SMBs may never need SOC 2. A seed-stage company selling to banks may need it urgently.

So stage and employee count are **extracted as evidence when present, but not scored**. The rubric scores the *observable causes* of compliance demand instead.

### The five dimensions (100 points)

Each dimension gets a level of 0, 1 or 2. Points = weight × level ÷ 2.

| Dimension | Weight | Why it matters for Sprinto | Level 1 | Level 2 |
|---|---|---|---|---|
| **Business & technology fit** | 25 | Sprinto only works for companies running software that holds customer data. Without that, there's nothing to certify. | Software/tech service, delivery unclear | Explicit cloud software or digital service handling customer data |
| **Customer trust exposure** | 20 | Enterprise and regulated buyers are what *force* SOC 2 / ISO 27001: they send security questionnaires and block deals without certification. This is the "pressure from enterprise customers" part of the ICP. | B2B, buyer details limited | Explicit enterprise offering, named enterprise customers, or regulated segments |
| **Relevant compliance workload** | 25 | The direct signal that Sprinto's work exists here: audits, frameworks, ongoing controls, trust centres. Weighted equal-highest with business fit because it's the closest proxy for "preparing for an audit". | Security documentation or broad compliance mentions | Explicit audit, framework, certification, ongoing controls or customer-assurance activity |
| **Reason to engage now** | 20 | Timing makes an email land: an active audit, security/compliance hiring, a compliance deadline. It's weighted below fit because a great-fit company is still worth contacting without a dated trigger. | Dated expansion or enterprise launch within 12 months | Audit initiative, relevant hiring or compliance deadline within 6 months |
| **Delivery complexity** | 10 | Several frameworks, products or environments make manual compliance painful, which is where automation pays off most. Weighted lowest because it raises value, not relevance. | Integrations or several products | Documented multiple frameworks, environments or products |

### Qualification rule

A company is **QUALIFIED** only when all three hold:

1. **Total ≥ 65.** A company that's strong on fit, trust exposure and compliance workload (25 + 20 + 25 = 70) qualifies even with no dated trigger. One that's weak on fit or compliance can't reach 65 on timing and complexity alone.
2. **Business fit = 2.** Explicit software handling customer data is non-negotiable: Sprinto can't help otherwise, however good the other signals look.
3. **Compliance workload ≥ 1.** There must be at least some evidence that compliance work exists. Without it, the email would be a guess.

### Routing when a company doesn't qualify

| Outcome | When |
|---|---|
| **Research more** | A dimension is 0 because evidence is *missing* (not negative), and filling it could change the outcome. The agent fetches pages that answer that specific question: product, customers, security/trust or careers/news. That's up to 3 extra rounds of up to 2 pages. |
| `INSUFFICIENT_EVIDENCE` | Still missing decision-relevant evidence after the research budget is spent. |
| `LOW_PRIORITY` | Evidence is there but below the threshold, and even the best case for the missing parts couldn't reach 65. |
| `DISQUALIFIED` | Explicitly not a software business, or a direct compliance-automation competitor (Vanta, Drata, Secureframe, Scrut, Sprinto and similar). |
| `RETRIEVAL_FAILED` | The site couldn't be read (blocked, bot challenge, empty). This is **never** treated as poor fit. |

### Evidence rules: what doesn't count as a signal

- **No badge found ≠ no compliance.** A missing SOC 2 logo is "unknown", not a deficiency to pitch against.
- **An existing SOC 2 / ISO 27001 does not disqualify.** Certified companies still have renewals, continuous controls, evidence collection and customer reviews. That's exactly Sprinto's ongoing work. atlan.com, for example, qualified at 90.
- **Funding ≠ budget; enterprise customers ≠ delayed deals; no team page ≠ no security team; first-party claims ≠ independent verification.**
- **Undated timing signals score 0.** "We're hiring" without a date isn't a reason to engage *now*.
- **Every fact needs a source URL and an exact quote that code finds on the page.** Otherwise the fact is downgraded to "unknown" and can't support a score.

### From the right company to the right person, pains and email

- **Who to contact** (10-point score: company association, functional ownership, ability to drive action, match to need, identity quality; pass ≥ 8). Every Hunter candidate is scored, never just the first. Preference order: compliance/GRC leader, then security leader/CISO, then CTO/VP Engineering when no security owner exists, then a technical founder only where the company is evidently founder-led. That order follows who actually owns an audit. At SpotDraft, no security or compliance owner appeared in Hunter's results, so the CTO was chosen. The CEO, COO and Head of Legal each scored 7/10 and didn't pass.
- **Pains** (each ≥ 7/10, two distinct): each must rest on a verified fact about *that* company, matter to *that* person's role, and map to one of Sprinto's four approved capabilities: compliance preparation, evidence collection, continuous control monitoring, customer trust documentation. They're always worded as hypotheses. The agent never invents manual work, lost revenue, urgency or dissatisfaction.
- **Email** (QA ≥ 8/10 plus mandatory checks, up to 2 revisions): one verified hook, one tentative pain, one capability, one easy question, addressed by name, about 80–120 words.

### How the definition behaved on the test list

The 9 domains in `domains.txt` cover strong fits, a certified company, an ambiguous case, two competitors and a non-SaaS business. Latest recorded outcomes:

| Domain | Total | Outcome | Why |
|---|---|---|---|
| spotdraft.com | 80 | Qualified → **email sent** | Cloud CLM handling contract data; named enterprise customers; SOC 2 Type II, ISO 27001, GDPR, HIPAA |
| everstage.com | 75 | Qualified → **email sent** | Cloud incentive-compensation (sales commission) platform for enterprises; ISO 27001:2022, GDPR data-transfer clauses, India's DPDP Act |
| rocketlane.com | 80 | Qualified; no contact passed 8/10 | Enterprise PSA/onboarding platform with SOC 2 Type II, ISO 27001 and more (two extra research rounds); live Hunter results had no qualifying owner |
| zluri.com | 80 | Qualified; no contact passed 8/10 | Strong fit; same contact outcome |
| atlan.com | 90 | Qualified; no contact passed 8/10 | Already certified, and **not** auto-disqualified, by design |
| hevodata.com | 62.5 | `INSUFFICIENT_EVIDENCE` | Borderline: scored 75 (qualified) in an earlier run. Model judgement on a thin site varies between runs, which is why it's routed to "insufficient evidence", not rejected. |
| vanta.com | – | `DISQUALIFIED` | Compliance-automation competitor, supported by page evidence |
| drata.com | – | `DISQUALIFIED` | Competitor: its own site describes a compliance-automation and trust-management platform (page evidence plus the configured competitor list) |
| bewakoof.com | 0 | `DISQUALIFIED` | Explicitly a consumer clothing retailer |

Every outcome is traceable in the dashboard's company pages: evidence with quotes, the five levels with reasons and any code adjustments, each research round, every contact candidate, both pains and every email draft with its QA scores.

## Setup

```bash
git clone https://github.com/varshithreddy-tech/sprinto-agent.git
cd sprinto-agent
uv venv --python 3.12 .venv          # or: python -m venv .venv
.venv\Scripts\activate               # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-local.txt   # requirements.txt is the slim Vercel set
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
| `DEADLINE_CHECK_MINUTES` | `10`: how often the dashboard / `--watch` checks for overdue threads |

## How to run

```bash
# Full offline-ish dry run: live website research + Gemini, fixture Hunter data, no real email
python run.py --domains domains.txt --dry-run --mock-hunter

# Live: real Hunter + real Resend send to DEMO_EMAIL
python run.py --domains domains.txt

# No-reply deadline (48h): escalate overdue threads once, or keep watching
python run.py --check-deadlines
python run.py --watch            # every DEADLINE_CHECK_MINUTES (default 10); --watch 5 for every 5 min

# Dashboard
streamlit run app.py

# Tests (scoring, reply agent, failure handling — no API calls)
python -m pytest -q
```

Domains already sent are skipped on re-run (resume after a restart). Add `--force` to process them again; the stable operation ID still blocks duplicate sends.

## How to demo

1. **Run** page: paste domains and click **Run**. Mock Hunter and Dry run are **on by default**; switching either off shows a LIVE warning (real Hunter credits / real emails). Progress updates live for each domain.
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
- Every send gets an operation ID that is stored *before* sending, along with the original request. On a timeout, or when resuming a send interrupted by a crash, the agent **first asks Resend whether that exact email already went out** (see below). It only retries if not, always with the same ID as the idempotency key, so a send that did land can't be duplicated. An outcome that still can't be confirmed is `SEND_UNCERTAIN`.
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
12. **Email is sent through Resend, not Gmail.** The case study says to send via Gmail. This build uses Resend so it doesn't need a stored Gmail password or a Google OAuth setup. The outcome the case asks for is unchanged: every email is hard-coded to your own address (`DEMO_EMAIL`, enforced in code, CC/BCC always empty), and it lands in your inbox. On Resend's free tier, emails come from `onboarding@resend.dev`.

## No-reply deadline automation

A thread waiting on the prospect is escalated when its deadline passes. Waiting statuses are `AWAITING_REPLY`, `TIMES_PROPOSED`, `INFO_SENT`, `CLARIFICATION_REQUESTED` and `OUT_OF_OFFICE`. Escalation pauses only that prospect. The deadline resets to 48h after every agent message, and a booked meeting never times out. The check runs automatically in three places:

- **Dashboard:** a background thread checks every `DEADLINE_CHECK_MINUTES` while `streamlit run app.py` is open. The Threads page shows the last check and each thread's time left.
- **Every batch run:** the sweep runs after the last domain.
- **`python run.py --watch`:** a headless loop, e.g. in a terminal you leave open.

The check is idempotent (escalated threads are no longer waiting). It only touches threads that are actually due, and one thread's failure doesn't stop the others. To run it even when nothing is open, `scripts/schedule_deadline_check.ps1` registers an hourly Windows Task Scheduler job. It is **not** registered automatically; run it yourself, and remove it with `-Remove`.

## Activity-log failures (rubric 3.11: "preserve successful actions; retry logging only")

- **Every log entry gets a stable id and its original timestamp when it is created.** Writes happen in one transaction, so a batch is written fully or not at all. Re-writing an id is ignored, so a retry can never duplicate a row.
- **Reply agent:** if the write fails, the send or booking still stands. The entries stay in the thread's LangGraph checkpoint (`crm_logged=False`) and are carried forward, never wiped, until they are written.
- **Pipeline:** logging can never turn a successful send into an error. After 3 failed attempts, an entry goes to `data/activity_spool.jsonl` (gitignored with `data/`).
- **Retry sweep:** `retry_failed_logging()` writes the spool plus any unlogged thread entries, and never re-runs sends or bookings. It runs in the same places as the deadline check: the dashboard watcher, the end of every batch, and `run.py --check-deadlines` / `--watch`. The **Activity log** page shows what is waiting and has a "Retry failed log writes now" button.

## Send reconciliation (rubric 3.11: "send timeout: reconcile provider state before retrying")

1. **On a timeout,** `send_once` looks the email up in Resend: same recipient and subject, sent after the operation started (with 2 minutes of clock-skew allowance), and **exactly the same text**.
   - **Found:** it is recorded as SENT with Resend's ID. No retry.
   - **Not found:** it is retried with the same idempotency key.
2. **On resume** (an operation left `PENDING` by a crash, or `SEND_UNCERTAIN`), the same lookup runs *before* anything is sent again.
3. **When the lookup isn't possible,** the agent relies on Resend's idempotency key, which dedupes the same request for 24 hours. A lookup *error* is treated as unknown, never as "not sent". The method used is stored in the operation's `detail`.
   - **Your current key is send-only** ("This API key is restricted to only send emails"), so it can't look emails up. Create a **Full access** key in Resend and put it in `RESEND_API_KEY` to enable lookups.
4. **A sweep** runs on the dashboard watcher, `--check-deadlines` and `--watch`. It re-checks `SEND_UNCERTAIN` and stale `PENDING` operations, and marks the ones it finds as SENT. A reconciled first email also updates the company to `EMAIL_SENT` and opens its reply thread. The sweep **never re-sends**: anything not found stays uncertain for a human, and appears under "Sends needing reconciliation" on the Activity log page.

## Deployment (Vercel + Neon)

Live app: **https://sprinto-agent-nu.vercel.app** (`sprinto-agent.vercel.app` was already taken on Vercel). It is **open for review: no login** (`PUBLIC_ACCESS=1` in Vercel), so anyone with the link can view every page, send simulated prospect replies and queue runs. Email addresses (the demo inbox, prospects' research-only emails) are hidden on the pages, and actions are rate-limited (40 simulated replies and 30 queued domains per hour). Remove `PUBLIC_ACCESS` in Vercel to bring back the `APP_PASSWORD` login. **Queued runs are only processed while the research worker is running** (`python run.py --worker`).

How the components map onto the deployment:

| Component | Where it runs |
|---|---|
| Growth Lead UI | **Vercel**: FastAPI web app (`sprinto_web/app.py`, entry `api/index.py`): Run, Companies + full decision logs, Threads with the simulated-reply box, Calendar, Activity log |
| Reply agent + demo calendar | **Vercel functions**: LangGraph with a **PostgresSaver** checkpointer; Gemini classifies and drafts; Resend sends to DEMO_EMAIL |
| Database | **Neon Postgres** (`sprinto-agent`, Singapore): every table, the job queue and the LangGraph checkpoints |
| Research pipeline (scrape → qualify → Hunter → pains → email QA → send) | **Research worker**: `python run.py --worker` on a machine that can run headless Chromium for minutes per domain. It claims domains queued from the Vercel Run page and writes results back to Neon. This step can't run on Vercel (function time limit, Chromium size). |
| Maintenance (log retry, send reconciliation, no-reply deadlines) | Throttled on page visits (every 10 min at most), the worker, and a daily **Vercel Cron** hitting `/tasks/cron` (authorised by `CRON_SECRET`) |

**Auto-deploys:** the Vercel project is connected to this GitHub repo, so every push to `main` redeploys. Environment variables live in Vercel, not the repo: `GEMINI_API_KEY`, `GEMINI_MODEL`, `RESEND_API_KEY`, `DEMO_EMAIL`, `ORGANIZER_TZ`, `REPLY_DEADLINE_HOURS`, `DATABASE_URL`, `APP_PASSWORD`, `SESSION_SECRET` and `CRON_SECRET`. Vercel installs only the slim `requirements.txt`; `.vercelignore` keeps the scraper, research code, Streamlit app and tests out of the function.

**Using it:**

1. Open the site and log in.
2. On your laptop, start the worker:

   ```bash
   python run.py --worker
   ```
3. Queue domains on **Run**. Progress appears live, and the Run page shows whether the worker is online.
4. Open **Threads** and type simulated prospect replies. The reply agent runs on Vercel.

Local options: the same web app runs with `uvicorn sprinto_web.app:app --port 8600`. The Streamlit dashboard (`streamlit run app.py`) also still works; both use Neon when `DATABASE_URL` is set, otherwise `data/sprinto.db`.

**Tests:** `python -m pytest -q` runs everything on SQLite. `python -m pytest -q --pg` runs the same suite against Postgres on the Neon `ci-tests` branch (`TEST_DATABASE_URL`), each test in a throwaway schema. `scripts/migrate_sqlite_to_pg.py` copies a local SQLite database, including thread states, into Neon.
