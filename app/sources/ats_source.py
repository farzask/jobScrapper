"""ATS sweep source: probe once, then fetch known boards every run.

Slug discovery is expensive (many HTTP probes per company) but only has to
happen once -- results persist to the companies table, so coverage compounds
across runs instead of being rediscovered nightly.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import yaml
from sqlmodel import select

from app.config import ROOT
from app.models import Company, session, utcnow
from app.normalize import company_slug
from app.sources.ats import fetch_board, probe_company
from app.sources.base import RawJob, Source

log = logging.getLogger(__name__)

SEEDS = ROOT / "seeds" / "companies.yaml"


def seed_companies() -> list[str]:
    if not SEEDS.exists():
        return []
    data = yaml.safe_load(SEEDS.read_text(encoding="utf-8")) or {}
    names: list[str] = []
    for group in data.values():
        if isinstance(group, list):
            names.extend(str(n) for n in group)
    return names


def known_boards() -> list[tuple[str, str, str]]:
    """(company_name, ats_type, ats_slug) for every already-probed company."""
    with session() as sess:
        rows = sess.exec(
            select(Company).where(Company.ats_type.is_not(None))  # type: ignore[union-attr]
        ).all()
        return [(c.name, c.ats_type, c.ats_slug) for c in rows
                if c.ats_type and c.ats_slug]


def unprobed(names: list[str]) -> list[str]:
    """Names with no probe result recorded yet."""
    with session() as sess:
        out = []
        for n in names:
            c = sess.exec(select(Company).where(Company.slug == company_slug(n))).first()
            if c is None or (c.ats_type is None and c.enriched_at is None):
                out.append(n)
        return out


def _record(name: str, ats: str | None, slug: str | None) -> None:
    """Persist a probe result. enriched_at marks 'probed', including misses,
    so we don't re-probe a company that simply isn't on a supported ATS."""
    with session() as sess:
        cs = company_slug(name)
        c = sess.exec(select(Company).where(Company.slug == cs)).first()
        if c is None:
            c = Company(name=name, slug=cs)
        c.ats_type, c.ats_slug = ats, slug
        c.enriched_at = utcnow()
        if ats and slug:
            c.careers_url = {
                "greenhouse": f"https://boards.greenhouse.io/{slug}",
                "lever": f"https://jobs.lever.co/{slug}",
                "ashby": f"https://jobs.ashbyhq.com/{slug}",
                "workable": f"https://apply.workable.com/{slug}",
                "smartrecruiters": f"https://jobs.smartrecruiters.com/{slug}",
                "recruitee": f"https://{slug}.recruitee.com",
            }.get(ats)
        sess.add(c)
        sess.commit()


class ATSSweep(Source):
    """Fetches every known company board, probing new companies first."""

    name = "ats"
    CONCURRENCY = 8

    async def fetch(self, client: httpx.AsyncClient, cfg) -> list[RawJob]:
        names = seed_companies()
        todo = unprobed(names)
        if todo:
            log.info("probing %d unseen companies for ATS boards", len(todo))
            await self._probe_all(client, todo)

        boards = known_boards()
        log.info("sweeping %d known ATS boards", len(boards))

        sem = asyncio.Semaphore(self.CONCURRENCY)

        async def one(company: str, ats: str, slug: str) -> list[RawJob]:
            async with sem:
                return await fetch_board(client, ats, slug, company)

        results = await asyncio.gather(
            *(one(c, a, s) for c, a, s in boards), return_exceptions=True)

        out: list[RawJob] = []
        for r in results:
            if isinstance(r, list):
                out.extend(r)
        return out

    async def _probe_all(self, client: httpx.AsyncClient, names: list[str]) -> None:
        sem = asyncio.Semaphore(4)      # probing is request-heavy; go gently

        async def one(name: str) -> None:
            async with sem:
                try:
                    hit = await probe_company(client, name)
                except Exception as exc:  # noqa: BLE001
                    log.debug("probe %s errored: %s", name, exc)
                    hit = None
                _record(name, hit[0] if hit else None, hit[1] if hit else None)

        await asyncio.gather(*(one(n) for n in names))
