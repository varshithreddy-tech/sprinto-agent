"""Single-fetch scraper for a company's own public website.

httpx first; headless Chromium (Playwright) only when the page is a JS shell or < 200 words.
Never bypasses CAPTCHAs / bot protection: those are recorded as RETRIEVAL_FAILED.
"""
from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag

import httpx

from config import settings as S
from scraper import cache, robots
from scraper.clean import clean_html, is_js_shell

OK, FAILED, NOT_FOUND, DISALLOWED = "OK", "RETRIEVAL_FAILED", "NOT_FOUND", "ROBOTS_DISALLOWED"
BOT_MARKERS = ("captcha", "cf-challenge", "challenge-platform", "attention required! | cloudflare",
               "just a moment...", "verify you are human", "px-captcha", "are you a robot")


class TransientError(Exception):
    pass


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def normalize_url(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{p.netloc.lower()}{path}" + (f"?{p.query}" if p.query else "")


def extract_links(html: str, base: str) -> list[str]:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
    out = []
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append(normalize_url(urljoin(base, href)))
    return list(dict.fromkeys(out))


class Fetcher:
    """One instance per domain. Tracks fetched URLs (no URL fetched twice per run) and politeness delay."""

    def __init__(self, domain: str, client: httpx.Client | None = None, use_playwright: bool = True,
                 use_cache: bool = True, sleep=time.sleep):
        self.domain = domain
        self.client = client or httpx.Client(headers={"User-Agent": S.USER_AGENT}, timeout=S.HTTP_TIMEOUT_S,
                                             follow_redirects=True)
        self.use_playwright = use_playwright
        self.use_cache = use_cache
        self.sleep = sleep
        self.fetched: dict[str, dict] = {}
        self._last_request = 0.0
        self._pw = None
        self._browser = None

    # ---- politeness -------------------------------------------------------
    def _wait(self) -> None:
        delay = random.uniform(*S.SAME_DOMAIN_DELAY_S)
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < delay:
            self.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    # ---- raw fetch ----------------------------------------------------------
    def _http_get(self, url: str) -> httpx.Response:
        self._wait()
        try:
            r = self.client.get(url)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as e:
            raise TransientError(f"{type(e).__name__}: {e}") from e
        if r.status_code in (429, 500, 502, 503, 504):
            raise TransientError(f"HTTP {r.status_code}")
        return r

    def get_raw(self, url: str) -> tuple[httpx.Response | None, str | None, int]:
        """Up to MAX_FETCH_ATTEMPTS total for transient errors with bounded backoff."""
        last_err = None
        for attempt in range(1, S.MAX_FETCH_ATTEMPTS + 1):
            try:
                return self._http_get(url), None, attempt
            except TransientError as e:
                last_err = str(e)
                if attempt < S.MAX_FETCH_ATTEMPTS:
                    self.sleep(min(2 ** attempt, 8))
        return None, f"transient failure after {S.MAX_FETCH_ATTEMPTS} attempts: {last_err}", S.MAX_FETCH_ATTEMPTS

    def _playwright_html(self, url: str) -> str | None:
        try:
            from playwright.sync_api import sync_playwright
            if self._browser is None:
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=True)
            page = self._browser.new_page(user_agent=S.USER_AGENT)
            try:
                self._wait()
                page.goto(url, timeout=S.PLAYWRIGHT_TIMEOUT_MS, wait_until="networkidle")
                return page.content()
            finally:
                page.close()
        except Exception:  # timeout or launch failure: fall back to httpx result
            return None

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._browser = self._pw = None

    # ---- public --------------------------------------------------------------
    def fetch(self, url: str, reason: str = "") -> dict:
        url = normalize_url(url)
        if url in self.fetched:
            return self.fetched[url]
        page = {"url": url, "domain": self.domain, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "retrieval_date": _today(), "reason": reason, "attempts": 0, "text": "", "word_count": 0,
                "dates": [], "links": [], "method": None, "error": None}
        if self.use_cache and (hit := cache.get(url)):
            hit.update(reason=reason, retrieval_date=hit["retrieved_at"][:10], attempts=0)
            self.fetched[url] = hit
            return hit
        if not robots.allowed(url):
            page.update(status=DISALLOWED, error="disallowed by robots.txt")
            self.fetched[url] = page
            return page

        resp, err, attempts = self.get_raw(url)
        page["attempts"] = attempts
        if resp is None:
            page.update(status=FAILED, error=err)
            self.fetched[url] = page
            return page
        html = resp.text or ""
        lower = html[:20000].lower()
        if resp.status_code == 404:
            page.update(status=NOT_FOUND, error="HTTP 404")
        elif resp.status_code in (401, 403):
            reason_txt = "bot protection / CAPTCHA" if any(m in lower for m in BOT_MARKERS) else f"HTTP {resp.status_code} forbidden"
            page.update(status=FAILED, error=reason_txt)
        elif resp.status_code >= 400:
            page.update(status=FAILED, error=f"HTTP {resp.status_code}")
        elif any(m in lower for m in BOT_MARKERS) and len(html) < 30000:
            page.update(status=FAILED, error="bot protection / CAPTCHA (not bypassed)")
        else:
            final_url = normalize_url(str(resp.url))
            text, dates = clean_html(html, final_url)
            method = "httpx"
            words = len(text.split())
            if self.use_playwright and (words < S.MIN_WORDS_BEFORE_JS_FALLBACK or is_js_shell(html)):
                rendered = self._playwright_html(url)
                if rendered:
                    t2, d2 = clean_html(rendered, final_url)
                    if len(t2.split()) > words:
                        html, text, dates, method, words = rendered, t2, d2, "playwright", len(t2.split())
            links = extract_links(html, final_url)
            if words == 0:
                page.update(status=FAILED, error="empty page after cleaning", method=method)
            elif words < 30 and re.search(r"enable javascript|loading application|loading\.\.\.", text, re.I):
                page.update(status=FAILED, error="JavaScript app did not render for headless browser", method=method)
            else:
                page.update(status=OK, method=method, text=text, word_count=words, dates=dates, links=links,
                            final_url=final_url)
        if page["status"] == OK:
            cache.put(page)
        self.fetched[url] = page
        return page
