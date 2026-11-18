"""Page discovery + classification into the rubric's question types."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from scraper import robots
from scraper.fetch import Fetcher, normalize_url

QUESTION_KEYWORDS: dict[str, list[str]] = {
    "compliance": ["security", "trust", "compliance", "privacy", "gdpr", "soc", "iso", "hipaa"],
    "buyer": ["customers", "customer-stories", "case-studies", "case-study", "enterprise", "industries", "success-stories"],
    "timing": ["careers", "jobs", "news", "blog", "press", "announcements", "newsroom", "funding", "media"],
    "ownership": ["about", "team", "leadership", "company", "founders"],
    "product": ["product", "products", "solutions", "platform", "features", "docs", "documentation", "integrations", "pricing"],
}
SKIP_RE = re.compile(r"\.(pdf|png|jpe?g|gif|svg|webp|zip|mp4|mp3|css|js|xml|ico)$|/(login|signin|sign-in|signup|sign-up|"
                     r"register|cart|checkout|account|wp-admin|cdn-cgi)(/|$)|/(tag|author|category|page)/", re.I)
LOCALE_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?)(?:/|$)", re.I)


def registrable(domain: str) -> str:
    d = domain.lower().removeprefix("www.")
    return d


NON_PROD_RE = re.compile(r"^(stage|staging|stg|dev|test|qa|uat|sandbox|preview|beta)[.-]", re.I)


def same_site(url: str, domain: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    base = registrable(domain)
    if NON_PROD_RE.match(host):
        return False  # staging/dev hosts are not the company's public site
    return host == base or host.endswith("." + base)


def classify(url: str) -> list[str]:
    p = urlparse(url)
    host_label = p.netloc.split(".")[0].lower()
    segs = [s for s in re.split(r"[/\-_.]", p.path.lower()) if s]
    path = p.path.lower()
    types = []
    if host_label in ("trust", "security"):
        types.append("compliance")
    if host_label in ("docs", "developer", "developers"):
        types.append("product")
    for q, kws in QUESTION_KEYWORDS.items():
        # whole-word match on URL segments only ("soc" must not match "/socks-for-men")
        if any(k in segs or re.search(rf"/{re.escape(k)}(?=[/\-_.]|$)", path) for k in kws):
            types.append(q)
    return list(dict.fromkeys(types))


def _priority(url: str, q: str) -> tuple:
    p = urlparse(url)
    depth = len([s for s in p.path.split("/") if s])
    host_bonus = 0 if p.netloc.split(".")[0] in ("www", registrable(p.netloc).split(".")[0]) else 1
    if q == "compliance" and p.netloc.startswith("trust."):
        host_bonus = -1
    exact = 0 if any(p.path.lower().rstrip("/").endswith("/" + k) for k in QUESTION_KEYWORDS[q]) else 1
    locale = 1 if LOCALE_RE.match(p.path) and depth > 1 else 0
    return (host_bonus, locale, exact, depth, len(url))


def parse_sitemap_xml(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def discover(fetcher: Fetcher, home: dict, max_sitemap_urls: int = 400) -> dict:
    """Return {"candidates": {question: [urls...]}, "sources": {...}, "trust_subdomain": url|None}."""
    domain = fetcher.domain
    urls = [u for u in home.get("links", []) if same_site(u, domain)]
    sources = {"homepage_links": len(urls), "sitemap_urls": 0}
    sm_urls = robots.sitemaps(home["url"]) or [f"https://{registrable(domain)}/sitemap.xml",
                                                f"https://www.{registrable(domain)}/sitemap.xml"]
    sitemap_found = []
    for sm in sm_urls[:2]:
        if not robots.allowed(sm):
            continue
        resp, _err, _ = fetcher.get_raw(sm)
        if resp is None or resp.status_code != 200 or "<loc>" not in resp.text:
            continue
        locs = parse_sitemap_xml(resp.text)
        # Sitemap index: open up to 3 child sitemaps that look relevant.
        children = [l for l in locs if l.endswith(".xml")]
        pages = [l for l in locs if not l.endswith(".xml")]
        for child in sorted(children, key=lambda c: (("page" not in c and "post" in c), len(c)))[:3]:
            r2, _, _ = fetcher.get_raw(child)
            if r2 is not None and r2.status_code == 200:
                pages += [l for l in parse_sitemap_xml(r2.text) if not l.endswith(".xml")]
        sitemap_found.append(sm)
        urls += [normalize_url(u) for u in pages[:max_sitemap_urls] if same_site(u, domain)]
        sources["sitemap_urls"] += len(pages[:max_sitemap_urls])
        break
    trust_url = None
    trust_candidate = f"https://trust.{registrable(domain)}"
    if robots.allowed(trust_candidate):
        resp, _err, _ = fetcher.get_raw(trust_candidate)
        if resp is not None and resp.status_code == 200 and len(resp.text) > 500:
            trust_url = normalize_url(str(resp.url))
            urls.append(trust_url)
    candidates: dict[str, list[str]] = {q: [] for q in QUESTION_KEYWORDS}
    for u in dict.fromkeys(urls):
        if SKIP_RE.search(urlparse(u).path) or u == home["url"]:
            continue
        for q in classify(u):
            candidates[q].append(u)
    for q in candidates:
        candidates[q] = sorted(set(candidates[q]), key=lambda u: _priority(u, q))[:40]
    return {"candidates": candidates, "sources": sources, "trust_subdomain": trust_url,
            "sitemaps": sitemap_found, "total_urls": len(set(urls))}


def pick_initial(candidates: dict[str, list[str]], n: int = 3) -> list[tuple[str, str]]:
    """Homepage + up to n: prefer one compliance, one buyer, one timing page."""
    picked: list[tuple[str, str]] = []
    for q in ("compliance", "buyer", "timing", "product", "ownership"):
        for u in candidates.get(q, []):
            if u not in [p[0] for p in picked]:
                picked.append((u, q))
                break
        if len(picked) >= n:
            break
    return picked


def pick_for_question(candidates: dict[str, list[str]], question: str, exclude: set[str], k: int) -> list[str]:
    return [u for u in candidates.get(question, []) if u not in exclude][:k]
