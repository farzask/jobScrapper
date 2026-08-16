"""Tier A -- open remote-job feeds. Free, keyless, no documented rate limits.

Field names below were read off live responses, not documentation, since
several of these feeds have docs that drifted from reality.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.sources.base import RawJob, Source


def _epoch(v) -> datetime | None:
    try:
        n = float(v)
        if n > 1e11:      # milliseconds
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(v) -> datetime | None:
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _salary(lo, hi, cur="USD") -> str | None:
    try:
        lo_i, hi_i = int(float(lo or 0)), int(float(hi or 0))
    except (TypeError, ValueError):
        return None
    if lo_i <= 0 and hi_i <= 0:
        return None
    if lo_i and hi_i:
        return f"{cur} {lo_i:,} - {hi_i:,}"
    return f"{cur} {(lo_i or hi_i):,}"


class RemoteOK(Source):
    name = "remoteok"
    URL = "https://remoteok.com/api"

    async def fetch(self, client: httpx.AsyncClient, cfg) -> list[RawJob]:
        data = (await client.get(self.URL)).json()
        out = []
        for j in data:
            # Index 0 is a legal/attribution notice, not a job.
            if not isinstance(j, dict) or not j.get("position"):
                continue
            tags = j.get("tags") or []
            out.append(RawJob(
                title=j["position"],
                company_name=j.get("company") or "Unknown",
                apply_url=j.get("apply_url") or j.get("url") or "",
                source=self.name,
                source_id=str(j.get("id") or ""),
                location=j.get("location") or "Remote",
                description=j.get("description"),
                salary=_salary(j.get("salary_min"), j.get("salary_max")),
                posted_at=_epoch(j.get("epoch")) or _iso(j.get("date")),
                remote_type="remote",
                location_restriction=j.get("location"),
                market="remote_global",
                extra={"tags": tags},
            ))
        return out


class Remotive(Source):
    name = "remotive"
    URL = "https://remotive.com/api/remote-jobs"

    async def fetch(self, client: httpx.AsyncClient, cfg) -> list[RawJob]:
        data = (await client.get(self.URL)).json()
        return [RawJob(
            title=j.get("title", ""),
            company_name=j.get("company_name") or "Unknown",
            apply_url=j.get("url") or "",
            source=self.name,
            source_id=str(j.get("id") or ""),
            location=j.get("candidate_required_location") or "Remote",
            description=j.get("description"),
            salary=j.get("salary") or None,
            posted_at=_iso(j.get("publication_date")),
            remote_type="remote",
            # Remotive states this explicitly -- the most reliable of the feeds.
            location_restriction=j.get("candidate_required_location"),
            restriction_explicit=bool(j.get("candidate_required_location")),
            market="remote_global",
            extra={"tags": j.get("tags") or [], "category": j.get("category")},
        ) for j in data.get("jobs", []) if j.get("title")]


class Arbeitnow(Source):
    name = "arbeitnow"
    URL = "https://www.arbeitnow.com/api/job-board-api"

    async def fetch(self, client: httpx.AsyncClient, cfg) -> list[RawJob]:
        data = (await client.get(self.URL)).json()
        out = []
        for j in data.get("data", []):
            if not j.get("title"):
                continue
            is_remote = bool(j.get("remote"))
            out.append(RawJob(
                title=j["title"].strip(),
                company_name=j.get("company_name") or "Unknown",
                apply_url=j.get("url") or "",
                source=self.name,
                source_id=j.get("slug"),
                location=j.get("location"),
                description=j.get("description"),
                posted_at=_epoch(j.get("created_at")),
                remote_type="remote" if is_remote else "onsite",
                location_restriction=j.get("location"),
                market="remote_global" if is_remote else "other",
                extra={"tags": j.get("tags") or []},
            ))
        return out


class Himalayas(Source):
    name = "himalayas"
    URL = "https://himalayas.app/jobs/api"

    async def fetch(self, client: httpx.AsyncClient, cfg) -> list[RawJob]:
        out: list[RawJob] = []
        # Paginated; a few pages is plenty and keeps us polite.
        for offset in (0, 100, 200):
            r = await client.get(self.URL, params={"limit": 100, "offset": offset})
            if r.status_code != 200:
                break
            jobs = r.json().get("jobs", [])
            if not jobs:
                break
            for j in jobs:
                # Both lists arrive with mixed types -- timezones are ints.
                restrictions = [str(x) for x in (j.get("locationRestrictions") or [])]
                tz = [str(x) for x in (j.get("timezoneRestrictions") or [])]
                restr = ", ".join(restrictions) if restrictions else "Worldwide"
                # Himalayas occasionally returns a placeholder for companyName
                # (literally "name"); fall back to the slug, which is reliable.
                cname = (j.get("companyName") or "").strip()
                if cname.lower() in {"", "name", "company", "n/a", "unknown", "-"}:
                    cname = (j.get("companySlug") or "unknown").replace("-", " ").title()
                out.append(RawJob(
                    title=j.get("title", ""),
                    company_name=cname,
                    apply_url=j.get("applicationLink") or j.get("guid") or "",
                    source=self.name,
                    source_id=str(j.get("guid") or ""),
                    location=restr,
                    description=j.get("description") or j.get("excerpt"),
                    salary=_salary(j.get("minSalary"), j.get("maxSalary"),
                                   j.get("currency") or "USD"),
                    posted_at=_iso(j.get("pubDate")) or _epoch(j.get("pubDate")),
                    remote_type="remote",
                    location_restriction=restr + (f" | TZ: {', '.join(tz)}" if tz else ""),
                    restriction_explicit=bool(restrictions),
                    market="remote_global",
                    extra={"seniority": j.get("seniority") or [],
                           "categories": j.get("categories") or []},
                ))
        return out


class Jobicy(Source):
    name = "jobicy"
    URL = "https://jobicy.com/api/v2/remote-jobs"

    async def fetch(self, client: httpx.AsyncClient, cfg) -> list[RawJob]:
        data = (await client.get(self.URL, params={"count": 100})).json()
        return [RawJob(
            title=j.get("jobTitle", ""),
            company_name=j.get("companyName") or "Unknown",
            apply_url=j.get("url") or "",
            source=self.name,
            source_id=str(j.get("id") or ""),
            location=j.get("jobGeo") or "Anywhere",
            description=j.get("jobDescription") or j.get("jobExcerpt"),
            salary=_salary(j.get("salaryMin"), j.get("salaryMax"),
                           j.get("salaryCurrency") or "USD"),
            posted_at=_iso(j.get("pubDate")),
            remote_type="remote",
            location_restriction=j.get("jobGeo"),
            restriction_explicit=bool(j.get("jobGeo")),
            market="remote_global",
            extra={"level": j.get("jobLevel"), "industry": j.get("jobIndustry") or []},
        ) for j in data.get("jobs", []) if j.get("jobTitle")]


ALL = [RemoteOK(), Remotive(), Arbeitnow(), Himalayas(), Jobicy()]
