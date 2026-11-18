"""Fact extraction (Gemini) + code-side evidence verification."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

import llm
from scraper.clean import excerpt_in_text, normalize

FIELDS = ["company_name", "product", "business_model", "customer_segments", "named_customers", "funding_stage",
          "employee_count", "data_handled", "compliance_claims", "hiring_signals", "business_triggers",
          "existing_compliance_tools", "leadership_people"]
LABELS = {"observed", "inferred", "unknown", "conflicting"}


class Fact(BaseModel):
    value: str
    label: str = "observed"
    source_url: str = ""
    exact_excerpt: str = ""
    retrieval_date: Optional[str] = None
    publication_date: Optional[str] = None
    signal_type: Optional[str] = None
    # code-filled
    field: Optional[str] = None
    verified: bool = False
    verification_note: Optional[str] = None
    id: Optional[str] = None
    round: int = 0

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        v = (v or "observed").lower().strip()
        return v if v in LABELS else "inferred"


class Extraction(BaseModel):
    facts: dict[str, list[Fact]] = Field(default_factory=dict)


def _pages_block(pages: list[dict]) -> str:
    return "\n\n".join(f'<page url="{p["url"]}" retrieved="{p.get("retrieval_date")}">\n{p["text"]}\n</page>' for p in pages)


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10] if fmt != "%Y-%m" else s[:7], fmt).date()
        except ValueError:
            continue
    return None


def date_supported(d: date, text: str) -> bool:
    """Is this publication date actually visible in the page text (any common format)?"""
    t = normalize(text)
    variants = {d.isoformat(), d.strftime("%B %d, %Y"), d.strftime("%b %d, %Y"), d.strftime("%d %B %Y"), d.strftime("%d %b %Y"), d.strftime("%Y/%m/%d"),
                f"{d.strftime('%B')} {d.day}, {d.year}", f"{d.strftime('%b')} {d.day}, {d.year}",
                f"{d.day} {d.strftime('%B')} {d.year}", f"{d.day} {d.strftime('%b')} {d.year}",
                f"{d.strftime('%b')}. {d.day}, {d.year}", f"{d.strftime('%B')} {d.year}"}
    return any(v and normalize(v) in t for v in variants)


def verify_facts(ex: Extraction, pages: list[dict], round_no: int) -> list[Fact]:
    """Every cited excerpt must appear in the cleaned text of the cited page, else -> unknown."""
    by_url = {p["url"]: p for p in pages}
    by_url.update({p.get("final_url"): p for p in pages if p.get("final_url")})
    out: list[Fact] = []
    for fld, facts in ex.facts.items():
        if fld not in FIELDS:
            continue
        for f in facts:
            f.field, f.round = fld, round_no
            page = by_url.get(f.source_url.rstrip("/")) or by_url.get(f.source_url) or by_url.get(f.source_url.rstrip("/") + "/")
            if page is None:
                # Model may cite a URL variant; accept only if excerpt is found in exactly that page's text elsewhere.
                page = next((p for p in pages if excerpt_in_text(f.exact_excerpt, p["text"])), None)
                if page:
                    f.verification_note = f"source_url corrected from {f.source_url}"
                    f.source_url = page["url"]
            f.retrieval_date = page.get("retrieval_date") if page else None
            if page and excerpt_in_text(f.exact_excerpt, page["text"]):
                f.verified = True
            else:
                f.verified = False
                f.label = "unknown"
                f.verification_note = "excerpt not found in cleaned page text: downgraded to unknown"
            if f.publication_date:
                d = parse_date(f.publication_date)
                if not d or not page or not date_supported(d, page["text"]):
                    f.verification_note = (f.verification_note or "") + f" | publication_date {f.publication_date} not visible on page: removed"
                    f.publication_date = None
                else:
                    f.publication_date = d.isoformat()
            out.append(f)
    return out


def extract(domain: str, pages: list[dict], round_no: int, today: str) -> tuple[list[Fact], dict]:
    ok_pages = [p for p in pages if p.get("status") == "OK" and p.get("text")]
    if not ok_pages:
        return [], {}
    ex, meta = llm.get_llm().json("extract", {"domain": domain, "retrieval_date": today, "pages": _pages_block(ok_pages)},
                                  Extraction)
    return verify_facts(ex, ok_pages, round_no), meta


def merge_facts(existing: list[Fact], new: list[Fact]) -> list[Fact]:
    """Keep all facts (conflicting evidence is preserved); drop exact duplicates; assign stable ids."""
    seen = {(f.field, normalize(f.value), f.source_url) for f in existing}
    merged = list(existing)
    for f in new:
        key = (f.field, normalize(f.value), f.source_url)
        if key in seen:
            continue
        seen.add(key)
        merged.append(f)
    for i, f in enumerate(merged, 1):
        f.id = f"F{i}"
    _mark_conflicts(merged)
    return merged


def _mark_conflicts(facts: list[Fact]) -> None:
    """Numeric single-valued fields (employee_count, funding_stage) with differing values -> conflicting."""
    for fld in ("employee_count", "funding_stage"):
        vals = [f for f in facts if f.field == fld and f.verified]
        norm = {re.sub(r"[^a-z0-9+]", "", normalize(f.value)) for f in vals}
        if len(norm) > 1:
            for f in vals:
                f.label = "conflicting"
