"""HTML -> clean text. trafilatura for main content, plus rule-based fallback.

Keeps JSON-LD dates and visible dates (needed for timing evidence); they are written into the
cleaned text itself so that cited excerpts can be verified against it.
"""
from __future__ import annotations

import json
import re
import unicodedata

import trafilatura
from bs4 import BeautifulSoup

from config import settings as S

DATE_KEYS = ("datePosted", "datePublished", "dateModified", "validThrough", "startDate")
MONTHS = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
VISIBLE_DATE_RE = re.compile(
    rf"\b(\d{{4}}-\d{{2}}-\d{{2}}|{MONTHS}\.? \d{{1,2}},? \d{{4}}|\d{{1,2}} {MONTHS},? \d{{4}})\b", re.I)
KEEP_SHORT_RE = re.compile(r"\b(soc ?[12]|iso ?27\d{3}|iso ?42001|gdpr|hipaa|pci|ccpa|dpdp|fedramp|certif|complian|audit|"
                           r"series [a-e]|raised|funding|employees|hiring|\d{4}-\d{2}-\d{2}|20\d{2})\b", re.I)
COOKIE_RE = re.compile(r"cookie|consent|gdpr-banner|onetrust|cc-window", re.I)


def is_js_shell(html: str) -> bool:
    soup_text = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S | re.I)
    visible = re.sub(r"<[^>]+>", " ", soup_text)
    words = len(visible.split())
    return words < 80 and ("__NEXT_DATA__" in html or 'id="root"' in html or 'id="app"' in html
                           or "window.__NUXT__" in html or html.count("<script") > 10)


def jsonld_dates(html: str) -> list[dict]:
    out = []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k in DATE_KEYS:
                    if isinstance(node.get(k), str):
                        out.append({"key": k, "value": node[k][:25],
                                    "type": str(node.get("@type", "")), "title": str(node.get("title") or node.get("headline") or node.get("name") or "")[:120]})
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    return out


def page_meta(html: str) -> list[str]:
    """<title> and meta description are always kept: they often state what the business is."""
    soup = BeautifulSoup(html[:200000], "html.parser")
    out = []
    if soup.title and soup.title.string and soup.title.string.strip():
        out.append(f"[Page title: {soup.title.string.strip()[:200]}]")
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content"):
            out.append(f"[Meta description: {m['content'].strip()[:300]}]")
            break
    return out


def _rule_based(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "svg", "form", "iframe"]):
        t.decompose()
    for t in soup.find_all(attrs={"class": COOKIE_RE}):
        t.decompose()
    for t in soup.find_all(attrs={"id": COOKIE_RE}):
        t.decompose()
    blocks = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "blockquote", "span", "div"]):
        if el.find(["p", "div", "li"]):
            continue  # leaf-ish blocks only
        txt = el.get_text(" ", strip=True)
        if txt:
            blocks.append(txt)
    return "\n".join(blocks)


def clean_html(html: str, url: str = "") -> tuple[str, list[dict]]:
    """Return (clean_text, dates). Text is capped at ~PAGE_TOKEN_CAP tokens."""
    main = trafilatura.extract(html, url=url or None, include_comments=False, include_tables=True,
                               favor_recall=True) or ""
    rules = _rule_based(html)
    # trafilatura can miss hero/marketing copy; merge both and de-duplicate blocks.
    raw_blocks = (main + "\n" + rules).splitlines()
    seen, blocks = set(), []
    short_fallback = []
    for b in raw_blocks:
        b = re.sub(r"\s+", " ", b).strip()
        key = b.lower()
        if key in seen or ("cookie" in key and len(b.split()) < 40):
            continue
        # Drop blocks under 8 words, EXCEPT short blocks carrying compliance/timing signal
        # (e.g. "SOC 2 Type II certified", "Series B", "Posted Oct 3, 2026").
        if len(b.split()) < S.MIN_BLOCK_WORDS and not KEEP_SHORT_RE.search(b):
            if len(b.split()) >= 2 and key not in seen:
                short_fallback.append(b)
            seen.add(key)
            continue
        seen.add(key)
        blocks.append(b)
    if len(" ".join(blocks).split()) < 30 and short_fallback:
        # Splash/storefront pages made only of short blocks: keep them rather than return an empty page.
        blocks += short_fallback[:80]
    meta = page_meta(html)
    dates = jsonld_dates(html)
    header = [m for m in meta if m.lower() not in seen]
    for d in dates[:15]:
        header.append(f"[JSON-LD {d['key']}={d['value']} {d['type']} {d['title']}]".strip())
    visible = sorted({m.group(0) for m in VISIBLE_DATE_RE.finditer(" ".join(blocks))})[:20]
    if visible:
        header.append("[Visible dates: " + "; ".join(visible) + "]")
    text = "\n".join(header + blocks)
    return cap_tokens(text), dates


def cap_tokens(text: str, cap: int = S.PAGE_TOKEN_CAP) -> str:
    words = text.split(" ")
    max_words = int(cap * 0.75)  # ~1.33 tokens per word
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " …[truncated]"


def normalize(s: str) -> str:
    """Whitespace/quote/case normalisation used for excerpt verification."""
    s = unicodedata.normalize("NFKC", s or "")
    s = (s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
         .replace("–", "-").replace("—", "-").replace(" ", " "))
    s = s.replace('"', "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def excerpt_in_text(excerpt: str, text: str) -> bool:
    e = normalize(excerpt).strip(" .…'")
    return bool(e) and len(e) >= 8 and e in normalize(text)
