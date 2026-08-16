"""Walk companies in the DB and fill in domain / LinkedIn / contact."""
from __future__ import annotations

import asyncio
import logging
import os

from sqlmodel import select

from app.enrich.company import enrich_one, hunter_optional, people_search_url
from app.models import Company, Contact, Job, session, utcnow
from app.sources.base import make_client

log = logging.getLogger("enrich")


def companies_needing_enrichment(limit: int = 200) -> list[tuple[int, str]]:
    """Companies that have at least one job but no resolved domain yet."""
    with session() as sess:
        job_company_ids = {j.company_id for j in sess.exec(select(Job)).all()
                           if j.company_id}
        rows = sess.exec(select(Company)).all()
        return [(c.id, c.name) for c in rows
                if c.id in job_company_ids and not c.domain][:limit]


async def enrich_all(limit: int = 200, concurrency: int = 6) -> dict:
    targets = companies_needing_enrichment(limit)
    if not targets:
        return {"enriched": 0, "with_domain": 0, "with_contact": 0}

    log.info("enriching %d companies", len(targets))
    hunter_key = os.environ.get("HUNTER_API_KEY")
    sem = asyncio.Semaphore(concurrency)
    results: list[tuple[int, dict]] = []

    async with make_client() as client:
        async def one(cid: int, name: str):
            async with sem:
                try:
                    info = await enrich_one(client, name)
                    results.append((cid, info))
                except Exception as exc:  # noqa: BLE001
                    log.debug("enrich %s failed: %s", name, exc)
                await asyncio.sleep(0.1)

        await asyncio.gather(*(one(cid, n) for cid, n in targets))

    with_domain = with_contact = 0
    with session() as sess:
        for cid, info in results:
            c = sess.get(Company, cid)
            if not c:
                continue
            c.domain = info["domain"]
            c.website = info["website"]
            # Never overwrite a LinkedIn URL harvested from a real job card.
            if not c.linkedin_url or info["linkedin_verified"]:
                c.linkedin_url = info["linkedin_url"]
            c.enriched_at = utcnow()
            sess.add(c)
            if info["domain"]:
                with_domain += 1

            paid = hunter_optional(info["domain"], hunter_key) if hunter_key else None

            existing = sess.exec(select(Contact).where(Contact.company_id == cid)).first()
            if existing is None and (info["contact_email"] or paid):
                if paid:
                    sess.add(Contact(
                        company_id=cid, name=paid["name"], title=paid["title"],
                        email=paid["email"], email_confidence=paid["email_confidence"],
                        linkedin_url=paid.get("linkedin_url") or info["people_search"],
                        source="hunter"))
                else:
                    sess.add(Contact(
                        company_id=cid,
                        name=None,
                        title="Hiring team (role inbox)",
                        email=info["contact_email"],
                        email_confidence=info["email_confidence"],
                        linkedin_url=info["people_search"],
                        source="pattern+mx"))
                with_contact += 1
        sess.commit()

    return {"enriched": len(results), "with_domain": with_domain,
            "with_contact": with_contact}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    res = asyncio.run(enrich_all())
    print(f"enriched={res['enriched']}  domain={res['with_domain']}  "
          f"contact={res['with_contact']}")


if __name__ == "__main__":
    main()
