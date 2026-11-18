"""Approved Sprinto capabilities. Pains and emails may only use these four areas.

`python -m outreach.capabilities --refresh` fetches sprinto.com pages with the same scraper and attaches
source URLs + verbatim excerpts to config/sprinto_capabilities.md.
"""
from __future__ import annotations

import re
import sys

from config import settings as S

CAP_FILE = S.CONFIG_DIR / "sprinto_capabilities.md"

CAPABILITIES = {
    "compliance preparation": "Guided readiness for SOC 2, ISO 27001, GDPR and other frameworks: scoping, policies, gap tracking and audit preparation.",
    "evidence collection": "Automated collection of audit evidence from connected cloud, HR and engineering systems instead of manual screenshots and spreadsheets.",
    "continuous control monitoring": "Ongoing automated checks that controls stay in place between audits, with alerts when something drifts.",
    "customer trust documentation": "Trust center and security documentation that helps answer customer security reviews and questionnaires.",
}
KEYWORDS = {
    "compliance preparation": ["audit-ready", "audit ready", "readiness", "policies", "get compliant", "soc 2", "iso 27001"],
    "evidence collection": ["evidence collection", "collect evidence", "evidence", "integrations"],
    "continuous control monitoring": ["continuous monitoring", "continuous control", "monitor", "real-time", "drift"],
    "customer trust documentation": ["trust center", "security questionnaire", "questionnaire", "trust"],
}


def load() -> dict[str, dict]:
    """Parse the md: {capability: {"description":..., "sources":[{"url","excerpt"}]}}."""
    out = {k: {"description": v, "sources": []} for k, v in CAPABILITIES.items()}
    if not CAP_FILE.exists():
        return out
    current = None
    for line in CAP_FILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^## (.+)$", line)
        if m and m.group(1).strip().lower() in out:
            current = m.group(1).strip().lower()
            continue
        m = re.match(r"^- source: (\S+) — \"(.+)\"$", line)
        if m and current:
            out[current]["sources"].append({"url": m.group(1), "excerpt": m.group(2)})
    return out


def as_prompt_text() -> str:
    caps = load()
    lines = []
    for k, v in caps.items():
        src = "; ".join(s["url"] for s in v["sources"][:2]) or "sprinto.com"
        lines.append(f"- {k}: {v['description']} (source: {src})")
    return "\n".join(lines)


def write_md(sources: dict[str, list[dict]]) -> None:
    lines = ["# Approved Sprinto capability areas", "",
             "Pains and emails may ONLY reference these four areas. No pricing, guarantees, time savings or",
             "certification outcomes may be claimed. Sources below were fetched from sprinto.com with the project scraper",
             "(`python -m outreach.capabilities --refresh`).", ""]
    for k, desc in CAPABILITIES.items():
        lines += [f"## {k}", "", desc, ""]
        for s in sources.get(k, [])[:3]:
            lines.append(f"- source: {s['url']} — \"{s['excerpt']}\"")
        lines.append("")
    CAP_FILE.write_text("\n".join(lines), encoding="utf-8")


def refresh() -> dict[str, list[dict]]:
    from scraper import discover as D
    from scraper.fetch import OK, Fetcher
    f = Fetcher("sprinto.com")
    try:
        urls: list[str] = []
        blocked = []
        # sprinto.com serves a bot challenge to crawlers (never bypassed); docs.sprinto.com is public.
        for seed in ("https://sprinto.com", "https://docs.sprinto.com"):
            home = f.fetch(seed, "capabilities")
            if home["status"] != OK:
                blocked.append(f"{seed}: {home.get('error')}")
                continue
            urls.append(home["url"])
            pref = [u for u in home.get("links", []) if D.same_site(u, "sprinto.com")]
            pref = [u for u in pref if re.search(r"(evidence|monitor|trust|soc-?2|iso-?27001|questionnaire|"
                                                 r"policies|audit|control|integration|vendor)", u, re.I)]
            urls += sorted(set(pref), key=len)[:10]
        print("blocked:", blocked)
        found: dict[str, list[dict]] = {k: [] for k in CAPABILITIES}
        for u in urls:
            p = f.fetch(u, "capabilities")
            if p["status"] != OK:
                continue
            sentences = re.split(r"(?<=[.!?])\s+|\n", p["text"])
            for cap, kws in KEYWORDS.items():
                if len(found[cap]) >= 2:
                    continue
                for s in sentences:
                    s2 = s.strip()
                    if 6 <= len(s2.split()) <= 40 and any(k in s2.lower() for k in kws) and not s2.startswith("["):
                        found[cap].append({"url": p["url"], "excerpt": s2.replace('"', "'")})
                        break
        write_md(found)
        return found
    finally:
        f.close()


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        res = refresh()
        for k, v in res.items():
            print(k, "->", [s["url"] for s in v])
    else:
        print(as_prompt_text())
