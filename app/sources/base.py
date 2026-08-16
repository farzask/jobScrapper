"""Source adapter contract.

Every source returns a list of RawJob. Adapters are isolated: a source that
breaks (site redesign, rate limit, outage) logs and returns [] rather than
taking the whole run down with it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


@dataclass
class RawJob:
    """Normalized shape every adapter must produce."""
    title: str
    company_name: str
    apply_url: str
    source: str
    source_id: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    salary: Optional[str] = None
    posted_at: Optional[datetime] = None
    remote_type: Optional[str] = None
    # Raw text stating where the candidate may be based. This is the single
    # most valuable field we collect -- it decides Pakistan eligibility.
    location_restriction: Optional[str] = None
    # True when the feed gave a definitive country/region allowlist. If so,
    # Pakistan's absence from it means "no", not "unknown".
    restriction_explicit: bool = False
    market: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


class Source:
    """Base adapter. Subclasses implement fetch()."""

    name: str = "base"
    enabled_key: str | None = None

    async def fetch(self, client: httpx.AsyncClient, cfg) -> list[RawJob]:
        raise NotImplementedError

    async def safe_fetch(self, client: httpx.AsyncClient, cfg) -> tuple[str, list[RawJob], str | None]:
        """Never raises. Returns (name, jobs, error)."""
        try:
            jobs = await self.fetch(client, cfg)
            log.info("source %s -> %d jobs", self.name, len(jobs))
            return self.name, jobs, None
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            log.warning("source %s FAILED: %s: %s", self.name, type(exc).__name__, exc)
            return self.name, [], f"{type(exc).__name__}: {exc}"


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(45.0, connect=20.0),
        follow_redirects=True,
        # Transient connect timeouts otherwise cost us a whole source for the
        # run; retrying at the transport layer keeps one blip from doing that.
        transport=httpx.AsyncHTTPTransport(retries=2),
        headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"},
    )
