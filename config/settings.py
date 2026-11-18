"""Central configuration. Every weight, threshold and limit from the rubric lives here."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = CONFIG_DIR / "prompts"
DATA_DIR = ROOT / "data"
DECISION_LOG_DIR = DATA_DIR / "decision_logs"
DB_PATH = Path(os.getenv("SPRINTO_DB_PATH", DATA_DIR / "sprinto.db"))
# Postgres (Neon). When set, everything (app, worker, Vercel) uses it instead of the SQLite file.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ON_VERCEL = bool(os.getenv("VERCEL"))
# Web app login (local + Vercel) and cookie signing; CRON_SECRET guards the scheduled maintenance endpoint.
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")
# PUBLIC_ACCESS=1: no login — anyone with the link can view and use the app (assignment review). Email addresses
# are hidden on pages and actions are rate-limited.
PUBLIC_ACCESS = os.getenv("PUBLIC_ACCESS", "").strip().lower() in ("1", "true", "yes", "on")
RATE_LIMIT_REPLIES_PER_HOUR = int(os.getenv("RATE_LIMIT_REPLIES_PER_HOUR", "40"))
RATE_LIMIT_DOMAINS_PER_HOUR = int(os.getenv("RATE_LIMIT_DOMAINS_PER_HOUR", "30"))
RATE_LIMIT_SENDS_PER_HOUR = int(os.getenv("RATE_LIMIT_SENDS_PER_HOUR", "20"))
ACTIVITY_SPOOL_PATH = DATA_DIR / "activity_spool.jsonl"   # failed log writes wait here for retry
if ON_VERCEL:  # only /tmp is writable in a Vercel function
    ACTIVITY_SPOOL_PATH = Path("/tmp/activity_spool.jsonl")
    DECISION_LOG_DIR = Path("/tmp/decision_logs")
FIXTURES_DIR = ROOT / "tests" / "fixtures"

# ---- Keys / integrations -------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
DEMO_EMAIL = os.getenv("DEMO_EMAIL", "").strip()
RESEND_FROM = os.getenv("RESEND_FROM", "Sprinto Outbound Demo <onboarding@resend.dev>")
ORGANIZER_TZ = os.getenv("ORGANIZER_TZ", "Asia/Kolkata")
ORGANIZER_NAME = os.getenv("ORGANIZER_NAME", "Sprinto Growth Team")
REPLY_DEADLINE_HOURS = int(os.getenv("REPLY_DEADLINE_HOURS", "48"))
DEADLINE_CHECK_MINUTES = float(os.getenv("DEADLINE_CHECK_MINUTES", "10"))

RUBRIC_VERSION = (CONFIG_DIR / "rubric_version.txt").read_text().strip()

# ---- Scraper (Part 5) ---------------------------------------------------
USER_AGENT = "SprintoResearchBot/1.0 (demo project)"
HTTP_TIMEOUT_S = 15
PLAYWRIGHT_TIMEOUT_MS = 20_000
MIN_WORDS_BEFORE_JS_FALLBACK = 200
SAME_DOMAIN_DELAY_S = (1.0, 2.0)
MAX_FETCH_ATTEMPTS = 3
PAGE_TOKEN_CAP = 2000          # ~ tokens per cleaned page
MIN_BLOCK_WORDS = 8
CACHE_MAX_AGE_DAYS = 7

# ---- Research loop (3.4) ---------------------------------------------------
INITIAL_EXTRA_PAGES = 3
MAX_ADDITIONAL_ROUNDS = 3
MAX_NEW_PAGES_PER_ROUND = 2

# ---- Qualification (3.3) ---------------------------------------------------
DIMENSIONS = {
    "business_fit": 25,
    "trust_exposure": 20,
    "compliance_workload": 25,
    "reason_now": 20,
    "delivery_complexity": 10,
}
QUALIFY_MIN_TOTAL = 65
QUALIFY_BUSINESS_FIT_LEVEL = 2
QUALIFY_MIN_COMPLIANCE_LEVEL = 1
TIMING_L2_WINDOW_DAYS = 183   # 6 months
TIMING_L1_WINDOW_DAYS = 365   # 12 months
# Which rubric question type fills the gap for each dimension.
DIMENSION_QUESTION = {
    "business_fit": "product",
    "trust_exposure": "buyer",
    "compliance_workload": "compliance",
    "reason_now": "timing",
    "delivery_complexity": "product",
}
COMPETITOR_NAMES = ["vanta", "drata", "secureframe", "scrut", "sprinto", "thoropass", "hyperproof", "anecdotes"]
COMPETITOR_DOMAINS = ["vanta.com", "drata.com", "secureframe.com", "scrut.io", "sprinto.com",
                      "thoropass.com", "hyperproof.io", "anecdotes.ai"]

# ---- Contact (3.5) -----------------------------------------------------------
CONTACT_CRITERIA = ["company_association", "functional_ownership", "ability_to_drive", "match_to_need", "identity_quality"]
CONTACT_PASS_MIN = 8

# ---- Pains (3.6) ---------------------------------------------------------------
PAIN_MIN_SCORE = 7
PAIN_MIN_EVIDENCE = 2
APPROVED_CAPABILITIES = [
    "compliance preparation",
    "evidence collection",
    "continuous control monitoring",
    "customer trust documentation",
]

# ---- Email (3.7) -----------------------------------------------------------------
EMAIL_CRITERIA = ["specific_hook", "useful_hypothesis", "relevant_value", "simple_next_step", "clarity"]
EMAIL_PASS_MIN = 8
EMAIL_MAX_REVISIONS = 2
EMAIL_WORDS = (80, 120)
EMAIL_WORDS_TOLERANCE = (70, 140)

# ---- Reply / scheduling (3.8, 3.9) -------------------------------------------------
MAX_COUNTERPROPOSAL_ROUNDS = 2
WORKDAY_START_H = 9
WORKDAY_END_H = 18
MEETING_MINUTES = 30
SCHEDULING_HORIZON_DAYS = 30
SLOTS_TO_PROPOSE = 3


def prompt_version(name: str) -> str:
    """Short content hash of a prompt file, persisted next to every LLM output."""
    text = (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    return f"{name}@{hashlib.sha256(text.encode()).hexdigest()[:10]}"


def missing_keys(*names: str) -> list[str]:
    vals = {"GEMINI_API_KEY": GEMINI_API_KEY, "HUNTER_API_KEY": HUNTER_API_KEY,
            "RESEND_API_KEY": RESEND_API_KEY, "DEMO_EMAIL": DEMO_EMAIL}
    return [n for n in names if not vals.get(n)]
