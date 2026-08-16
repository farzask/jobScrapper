"""Pipeline orchestration: discover -> dedupe -> filter -> score -> persist."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import timezone

from sqlmodel import select

from app import normalize as N
from app.config import load
from app.models import Company, Job, Run, init_db, session, utcnow
from app.sources.base import RawJob, make_client
from app.sources import pakistan, remote_feeds
from app.sources.ats_source import ATSSweep

log = logging.getLogger("pipeline")

ALL_SOURCES = [*remote_feeds.ALL, ATSSweep(), *pakistan.ALL]


def enabled_sources(cfg):
    return [s for s in ALL_SOURCES if cfg.get(f"sources.{s.name}", False)]


async def discover(cfg) -> tuple[list[RawJob], dict]:
    """Run all enabled adapters concurrently, isolating failures."""
    srcs = enabled_sources(cfg)
    if not srcs:
        log.warning("no sources enabled in config.yaml")
        return [], {}

    async with make_client() as client:
        results = await asyncio.gather(*(s.safe_fetch(client, cfg) for s in srcs))

    jobs: list[RawJob] = []
    stats: dict[str, dict] = {}
    for name, got, err in results:
        stats[name] = {"count": len(got), "error": err}
        jobs.extend(got)
    return jobs, stats


def upsert_company(sess, name: str) -> Company:
    slug = N.company_slug(name)
    existing = sess.exec(select(Company).where(Company.slug == slug)).first()
    if existing:
        return existing
    c = Company(name=name, slug=slug)
    sess.add(c)
    sess.flush()
    return c


def persist(jobs: list[RawJob], cfg) -> tuple[int, int]:
    """Insert new jobs, refresh last_seen on ones we've seen before."""
    from app.skills import load_skills, passes_skill_filter

    new = updated = 0
    profile = load_skills()
    with session() as sess:
        for rj in jobs:
            h = N.dedupe_hash(rj)
            existing = sess.exec(select(Job).where(Job.dedupe_hash == h)).first()
            if existing:
                existing.last_seen = utcnow()
                sess.add(existing)
                updated += 1
                continue

            eligible, reason = N.classify_eligibility(rj)
            sc, sreason, matched = N.score(rj, cfg, profile)

            # Skill gate is opt-in (min_matches / require_core in skills.yaml).
            ok_skills, why = passes_skill_filter(matched, profile)
            if not ok_skills:
                continue

            comp = upsert_company(sess, rj.company_name)
            # LinkedIn job cards carry the real company LinkedIn URL. Capture
            # it here -- it beats anything we could reconstruct later.
            li = (rj.extra or {}).get("company_linkedin")
            if li and not comp.linkedin_url:
                comp.linkedin_url = li
                sess.add(comp)
            posted = rj.posted_at
            if posted and posted.tzinfo:
                posted = posted.astimezone(timezone.utc).replace(tzinfo=None)

            sess.add(Job(
                dedupe_hash=h,
                title=rj.title.strip(),
                company_id=comp.id,
                company_name=rj.company_name,
                location=rj.location,
                remote_type=rj.remote_type,
                eligible=eligible,
                eligibility_reason=reason,
                market=N.detect_market(rj, eligible),
                salary=rj.salary,
                description=(rj.description or "")[:20000] or None,
                apply_url=N.canonical_url(rj.apply_url),
                source=rj.source,
                source_id=rj.source_id,
                posted_at=posted,
                match_score=sc,
                score_reason=sreason,
                matched_skills=", ".join(matched) or None,
                status="scored",
            ))
            new += 1
        sess.commit()
    return new, updated


async def run_discovery(cfg=None) -> dict:
    cfg = cfg or load()
    init_db()

    with session() as sess:
        run = Run()
        sess.add(run)
        sess.commit()
        run_id = run.id

    raw, stats = await discover(cfg)
    log.info("fetched %d raw jobs from %d sources", len(raw), len(stats))

    kept: list[RawJob] = []
    dropped = {"filters": 0, "ineligible": 0}
    for j in raw:
        # Sources disagree about whether they report remoteness; settle it once
        # up front so filtering and market detection see a consistent value.
        j.remote_type = N.infer_remote_type(j)
        ok, _why = N.passes_hard_filters(j, cfg)
        if not ok:
            dropped["filters"] += 1
            continue
        elig, _ = N.classify_eligibility(j)
        if elig == "no" and cfg.get("eligibility.drop_ineligible", True):
            dropped["ineligible"] += 1
            continue
        if elig == "unknown" and not cfg.get("eligibility.keep_unknown", True):
            continue
        kept.append(j)

    deduped = N.dedupe(kept)
    log.info("after filters=%d, after dedupe=%d (dropped %s)",
             len(kept), len(deduped), dropped)

    new, updated = persist(deduped, cfg)

    with session() as sess:
        run = sess.get(Run, run_id)
        run.finished_at = utcnow()
        run.raw_count = len(raw)
        run.new_count = new
        run.kept_count = len(deduped)
        run.source_stats = json.dumps({**stats, "dropped": dropped})
        sess.add(run)
        sess.commit()

    return {"raw": len(raw), "kept": len(deduped), "new": new,
            "updated": updated, "dropped": dropped, "sources": stats}


def rescore_all(cfg=None) -> dict:
    """Recompute scores and skill matches for every stored job.

    Called after you edit your skills, so the existing database re-ranks
    instantly instead of forcing a fresh multi-minute scrape.
    """
    from app.skills import load_skills, passes_skill_filter

    cfg = cfg or load()
    profile = load_skills()
    changed = hidden = 0

    with session() as sess:
        jobs = list(sess.exec(select(Job)).all())
        for j in jobs:
            rj = RawJob(
                title=j.title, company_name=j.company_name,
                apply_url=j.apply_url, source=j.source,
                location=j.location, description=j.description,
                salary=j.salary, posted_at=j.posted_at,
                remote_type=j.remote_type, location_restriction=j.location,
            )
            sc, reason, matched = N.score(rj, cfg, profile)
            ok, why = passes_skill_filter(matched, profile)
            if not ok:
                # Park it rather than delete -- relaxing the filter brings it
                # straight back without another scrape.
                sc, reason = 0, f"filtered: {why}"
                hidden += 1
            if (j.match_score != sc or
                    (j.matched_skills or "") != ", ".join(matched)):
                changed += 1
            j.match_score = sc
            j.score_reason = reason
            j.matched_skills = ", ".join(matched) or None
            sess.add(j)
        sess.commit()

    return {"total": len(jobs), "changed": changed, "hidden": hidden}


def top_jobs(limit: int = 50, min_score: int | None = None):
    cfg = load()
    ms = cfg.get("search.min_score", 40) if min_score is None else min_score
    with session() as sess:
        return list(sess.exec(
            select(Job).where(Job.match_score >= ms)
            .order_by(Job.match_score.desc(), Job.posted_at.desc())
            .limit(limit)
        ))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="JobApplier pipeline")
    ap.add_argument("--discover", action="store_true", help="fetch + score jobs")
    ap.add_argument("--export", action="store_true", help="write CSV")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()

    if args.discover or not (args.discover or args.export):
        res = asyncio.run(run_discovery())
        print("\n=== DISCOVERY ===")
        for name, s in res["sources"].items():
            flag = "OK " if not s["error"] else "ERR"
            print(f"  {flag} {name:<12} {s['count']:>5}"
                  + (f"  {s['error'][:60]}" if s["error"] else ""))
        print(f"\n  raw={res['raw']}  kept={res['kept']}  new={res['new']}  "
              f"updated={res['updated']}  dropped={res['dropped']}")

        cfg = load()
        jobs = top_jobs(args.limit)
        target = cfg.get("search.target_jobs", 30)
        print(f"\n  jobs at/above score {cfg.get('search.min_score', 40)}: "
              f"{len(jobs)} (target {target}) "
              f"{'** TARGET MET **' if len(jobs) >= target else '-- below target'}")
        for j in jobs[:15]:
            print(f"    {j.match_score:>3}  {j.title[:44]:<44} {j.company_name[:20]:<20} "
                  f"[{j.eligible}] {j.source}")

    if args.export:
        from app.export import export_csv
        p = export_csv(limit=args.limit)
        print(f"\n  CSV -> {p}")


if __name__ == "__main__":
    main()
