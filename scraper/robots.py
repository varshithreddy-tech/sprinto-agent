"""robots.txt handling. Fetched once per host per run; disallowed paths are skipped."""
from __future__ import annotations

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from config import settings as S

_cache: dict[str, RobotFileParser | None] = {}


def _parser_for(host_root: str) -> RobotFileParser | None:
    if host_root in _cache:
        return _cache[host_root]
    rp = RobotFileParser()
    try:
        r = httpx.get(f"{host_root}/robots.txt", headers={"User-Agent": S.USER_AGENT},
                      timeout=S.HTTP_TIMEOUT_S, follow_redirects=True)
        if r.status_code == 200 and "text" in r.headers.get("content-type", "text/plain"):
            rp.parse(r.text.splitlines())
        elif r.status_code in (401, 403):
            # Standard interpretation: robots blocked => treat as disallow-all.
            rp.parse(["User-agent: *", "Disallow: /"])
        else:
            rp.parse([])  # no robots.txt => allowed
    except httpx.HTTPError:
        rp = None  # unknown; allow but note
    _cache[host_root] = rp
    return rp


def allowed(url: str) -> bool:
    p = urlparse(url)
    rp = _parser_for(f"{p.scheme}://{p.netloc}")
    if rp is None:
        return True
    return rp.can_fetch(S.USER_AGENT, url)


def sitemaps(url: str) -> list[str]:
    p = urlparse(url)
    rp = _parser_for(f"{p.scheme}://{p.netloc}")
    return list(rp.site_maps() or []) if rp else []


def reset() -> None:
    _cache.clear()
