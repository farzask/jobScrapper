"""Tier B -- public ATS job boards.

Six ATS platforms expose a company's full open roles as free JSON with no key
and no auth. This is the highest-signal tier we have: most of these postings
never reach an aggregator, so they are exactly the jobs a normal search misses.

Every URL pattern below was verified against a live company board.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from app.sources.base import RawJob, Source

log = logging.getLogger(__name__)

# ats_type -> URL template. Verified live.
PATTERNS: dict[str, str] = {
    "greenhouse":      "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever":           "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby":           "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "workable":        "https://apply.workable.com/api/v1/widget/accounts/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
    "recruitee":       "https://{slug}.recruitee.com/api/offers/",
}


def _strip_html(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", s).strip() or None


def _dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            n = float(v)
            if n > 1e11:
                n /= 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)
        s = str(v).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


# --------------------------------------------------------------------------
# Per-ATS parsers. Each returns list[RawJob]; all are defensive because a
# company can customize fields.
# --------------------------------------------------------------------------

def _parse_greenhouse(data, company: str) -> list[RawJob]:
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name") if isinstance(j.get("location"), dict) else j.get("location")
        out.append(RawJob(
            title=j.get("title", "").strip(),
            company_name=j.get("company_name") or company,
            apply_url=j.get("absolute_url", ""),
            source="ats:greenhouse", source_id=str(j.get("id") or ""),
            location=loc, description=_strip_html(j.get("content")),
            posted_at=_dt(j.get("first_published") or j.get("updated_at")),
            location_restriction=loc,
        ))
    return out


def _parse_lever(data, company: str) -> list[RawJob]:
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        loc = cats.get("location")
        wt = (j.get("workplaceType") or "").lower()
        out.append(RawJob(
            title=j.get("text", "").strip(),
            company_name=company,
            apply_url=j.get("hostedUrl") or j.get("applyUrl") or "",
            source="ats:lever", source_id=str(j.get("id") or ""),
            location=loc,
            description=j.get("descriptionPlain") or _strip_html(j.get("description")),
            posted_at=_dt(j.get("createdAt")),
            remote_type="remote" if wt == "remote" else ("hybrid" if wt == "hybrid" else None),
            location_restriction=loc,
            extra={"department": cats.get("department"), "commitment": cats.get("commitment")},
        ))
    return out


def _parse_ashby(data, company: str) -> list[RawJob]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        secondary = [s.get("location") for s in (j.get("secondaryLocations") or [])
                     if isinstance(s, dict) and s.get("location")]
        loc = j.get("location") or ""
        full_loc = ", ".join(filter(None, [loc, *secondary]))
        out.append(RawJob(
            title=(j.get("title") or "").strip(),
            company_name=company,
            apply_url=j.get("applyUrl") or j.get("jobUrl") or "",
            source="ats:ashby", source_id=str(j.get("id") or ""),
            location=full_loc or loc,
            description=j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml")),
            posted_at=_dt(j.get("publishedAt")),
            remote_type="remote" if j.get("isRemote") else None,
            location_restriction=full_loc or loc,
        ))
    return out


def _parse_workable(data, company: str) -> list[RawJob]:
    out = []
    name = data.get("name") or company
    for j in data.get("jobs", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("country")])) or j.get("location")
        out.append(RawJob(
            title=(j.get("title") or "").strip(),
            company_name=name,
            apply_url=j.get("application_url") or j.get("url") or j.get("shortlink") or "",
            source="ats:workable", source_id=str(j.get("shortcode") or j.get("id") or ""),
            location=loc, description=_strip_html(j.get("description")),
            posted_at=_dt(j.get("published_on") or j.get("created_at")),
            remote_type="remote" if j.get("telecommuting") else None,
            location_restriction=loc,
        ))
    return out


def _parse_smartrecruiters(data, company: str) -> list[RawJob]:
    out = []
    for j in data.get("content", []):
        loc_o = j.get("location") or {}
        loc = ", ".join(filter(None, [loc_o.get("city"), loc_o.get("region"),
                                      loc_o.get("country")]))
        cid = j.get("id")
        out.append(RawJob(
            title=(j.get("name") or "").strip(),
            company_name=(j.get("company") or {}).get("name") or company,
            apply_url=j.get("applyUrl") or f"https://jobs.smartrecruiters.com/{company}/{cid}",
            source="ats:smartrecruiters", source_id=str(cid or ""),
            location=loc, description=None,
            posted_at=_dt(j.get("releasedDate")),
            remote_type="remote" if loc_o.get("remote") else None,
            location_restriction=loc,
        ))
    return out


def _parse_recruitee(data, company: str) -> list[RawJob]:
    out = []
    for j in data.get("offers", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("country_code")]))
        out.append(RawJob(
            title=(j.get("title") or "").strip(),
            company_name=company,
            apply_url=j.get("careers_url") or j.get("url") or "",
            source="ats:recruitee", source_id=str(j.get("id") or ""),
            location=loc, description=_strip_html(j.get("description")),
            posted_at=_dt(j.get("published_at")),
            remote_type="remote" if (j.get("remote") or
                                     "remote" in (j.get("location") or "").lower()) else None,
            location_restriction=loc,
        ))
    return out


PARSERS = {
    "greenhouse": _parse_greenhouse, "lever": _parse_lever, "ashby": _parse_ashby,
    "workable": _parse_workable, "smartrecruiters": _parse_smartrecruiters,
    "recruitee": _parse_recruitee,
}


def slug_variants(name: str) -> list[str]:
    """Company name -> plausible ATS slugs, most likely first."""
    base = name.strip().lower()
    nospace = re.sub(r"[^a-z0-9]", "", base)
    hyphen = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    # Drop common suffixes: "Acme Technologies" also lives at "acme".
    short = re.sub(r"(technologies|technology|labs|inc|llc|ltd|limited|"
                   r"group|software|solutions|systems)$", "", nospace)
    seen, out = set(), []
    for v in (nospace, hyphen, short, name.strip()):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def fetch_board(client: httpx.AsyncClient, ats: str, slug: str,
                      company: str) -> list[RawJob]:
    """Fetch one company's board. Returns [] on any failure."""
    url = PATTERNS[ats].format(slug=slug)
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return []
        return PARSERS[ats](r.json(), company)
    except Exception as exc:  # noqa: BLE001
        log.debug("board %s/%s failed: %s", ats, slug, exc)
        return []


async def probe_company(client: httpx.AsyncClient, name: str,
                        limit_ats: list[str] | None = None
                        ) -> tuple[str, str, list[RawJob]] | None:
    """Find which ATS a company uses by trying each pattern.

    Returns (ats_type, slug, jobs) for the first pattern that yields jobs.
    Result is cached to the DB by the caller so this cost is paid once.
    """
    for ats in (limit_ats or PATTERNS):
        for slug in slug_variants(name):
            jobs = await fetch_board(client, ats, slug, name)
            if jobs:
                log.info("probe hit: %s -> %s/%s (%d jobs)", name, ats, slug, len(jobs))
                return ats, slug, jobs
            await asyncio.sleep(0.05)   # stay polite
    return None
