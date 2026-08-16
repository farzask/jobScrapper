"""Tier C -- Pakistan-focused sources.

LinkedIn is accessed only through its public logged-out "jobs-guest" endpoint,
the same one an anonymous visitor hits. We never automate a logged-in session
and we rate-limit deliberately; nothing here touches your account.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.sources.base import RawJob, Source

log = logging.getLogger(__name__)


def _rel_date(text: str) -> datetime | None:
    """'1 week ago' / '3 days ago' -> approximate timestamp."""
    m = re.search(r"(\d+)\s*(hour|day|week|month)", text or "", re.I)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    days = {"hour": 0, "day": 1, "week": 7, "month": 30}[unit] * n
    from datetime import timedelta
    return datetime.now(timezone.utc) - timedelta(days=days)


class LinkedInGuest(Source):
    """Public, unauthenticated LinkedIn job search."""

    name = "linkedin_guest"
    URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    DELAY = 2.0          # seconds between requests -- stay well under any limit
    PAGES = 3            # 10 results per page
    MAX_TITLES = 12      # request budget: MAX_TITLES * PAGES * len(SWEEPS)

    # (label, extra query params). f_WT=2 is LinkedIn's "remote" filter, so the
    # second sweep finds remote roles open to Pakistan-based candidates.
    SWEEPS = [
        ("onsite", {"location": "Pakistan"}),
        ("remote", {"location": "Pakistan", "f_WT": "2"}),
    ]

    async def fetch(self, client: httpx.AsyncClient, cfg) -> list[RawJob]:
        titles = cfg.get("search.titles", [])[:self.MAX_TITLES]
        out: list[RawJob] = []
        seen: set[str] = set()

        for title in titles:
            for _label, extra in self.SWEEPS:
                for page in range(self.PAGES):
                    text = await self._get(client, {
                        "keywords": title, "start": page * 10, **extra})
                    if text is None:
                        break

                    cards = BeautifulSoup(text, "lxml").select("div.base-card, li")
                    found = 0
                    for c in cards:
                        job = self._parse_card(c)
                        if job and job.apply_url not in seen:
                            seen.add(job.apply_url)
                            out.append(job)
                            found += 1
                    await asyncio.sleep(self.DELAY)
                    if found == 0:
                        break
        return out

    async def _get(self, client: httpx.AsyncClient, params: dict) -> str | None:
        """One request with a single backoff retry; None means stop paging."""
        for attempt in (0, 1):
            try:
                r = await client.get(self.URL, params=params)
            except httpx.HTTPError as exc:
                log.debug("linkedin fetch error: %s", exc)
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code == 429:            # backing off is the whole point
                await asyncio.sleep(10.0)
                continue
            if r.status_code != 200 or not r.text.strip():
                return None
            return r.text
        return None

    def _parse_card(self, c) -> RawJob | None:
        t = c.select_one(".base-search-card__title")
        comp = c.select_one(".base-search-card__subtitle")
        loc = c.select_one(".job-search-card__location")
        link = c.select_one("a.base-card__full-link") or c.select_one("a[href]")
        if not (t and link and link.get("href")):
            return None

        when = c.select_one("time")
        posted = None
        if when:
            dt_attr = when.get("datetime")
            if dt_attr:
                try:
                    posted = datetime.fromisoformat(dt_attr).replace(tzinfo=timezone.utc)
                except ValueError:
                    posted = None
            posted = posted or _rel_date(when.get_text(strip=True))

        location = loc.get_text(strip=True) if loc else "Pakistan"

        # The card links straight to the company's LinkedIn page. This is the
        # only authoritative company-LinkedIn source we get for free, so keep
        # it rather than guessing the slug later.
        li_company = None
        for a in c.select("a[href]"):
            href = a.get("href") or ""
            if "linkedin.com/company/" in href:
                li_company = href.split("?")[0]
                break

        return RawJob(
            title=t.get_text(strip=True),
            company_name=comp.get_text(strip=True) if comp else "Unknown",
            apply_url=link["href"].split("?")[0],
            source=self.name,
            location=location,
            posted_at=posted,
            location_restriction=location,
            remote_type=None,           # inferred downstream from the location
            extra={"company_linkedin": li_company} if li_company else {},
        )


class Mustakbil(Source):
    """Pakistani job board. Plain HTML, no bot protection."""

    name = "mustakbil"
    BASE = "https://www.mustakbil.com"
    PATHS = ["/jobs/pakistan", "/jobs/software-engineering",
             "/jobs/information-technology"]

    async def fetch(self, client: httpx.AsyncClient, cfg) -> list[RawJob]:
        out: list[RawJob] = []
        seen: set[str] = set()

        for path in self.PATHS:
            try:
                r = await client.get(urljoin(self.BASE, path))
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if "/jobs/job/" not in href:
                    continue
                title = a.get_text(strip=True)
                # The board renders a second "View job" anchor per listing.
                if not title or title.lower().startswith("view job"):
                    continue
                url = urljoin(self.BASE, href)
                if url in seen:
                    continue
                seen.add(url)

                card = a.find_parent(["div", "li", "article"])
                company, location = "Unknown", "Pakistan"
                if card:
                    txt = card.get_text(" ", strip=True)
                    mloc = re.search(r"\b(Karachi|Lahore|Islamabad|Rawalpindi|"
                                     r"Peshawar|Faisalabad|Multan|Quetta|Sialkot)\b",
                                     txt, re.I)
                    if mloc:
                        location = f"{mloc.group(1)}, Pakistan"

                out.append(RawJob(
                    title=title, company_name=company, apply_url=url,
                    source=self.name, location=location,
                    location_restriction=location,
                ))
            await asyncio.sleep(1.0)
        return out


ALL = [LinkedInGuest(), Mustakbil()]
