"""CSV export -- the deliverable format requested."""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from sqlmodel import select

from app.config import load
from app.models import Application, Company, Contact, Job, session

COLUMNS = [
    "job_title", "company", "company_linkedin", "company_website",
    "location", "remote_type", "eligible_from_pakistan", "eligibility_reason",
    "salary", "posted_date", "source", "application_link",
    "contact_name", "contact_title", "contact_email", "email_confidence",
    "contact_linkedin", "cold_email_draft",
    "match_score", "score_reason", "tailored_resume_path",
    "status", "applied_date", "notes",
]


def _clean(v) -> str:
    if v is None:
        return ""
    s = str(v)
    s = re.sub(r"<[^>]+>", " ", s)          # strip any stray HTML
    s = re.sub(r"\s+", " ", s).strip()
    return s


def export_csv(path: str | Path | None = None, limit: int = 500,
               min_score: int | None = None) -> Path:
    cfg = load()
    ms = cfg.get("search.min_score", 40) if min_score is None else min_score

    out_dir = cfg.path("output_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = Path(path) if path else out_dir / f"jobs_{datetime.now():%Y%m%d_%H%M}.csv"

    with session() as sess:
        jobs = list(sess.exec(
            select(Job).where(Job.match_score >= ms)
            .order_by(Job.match_score.desc())
            .limit(limit)
        ))

        rows = []
        for j in jobs:
            comp = sess.get(Company, j.company_id) if j.company_id else None
            contact = None
            if comp:
                contact = sess.exec(
                    select(Contact).where(Contact.company_id == comp.id)
                    .order_by(Contact.email_confidence)
                ).first()
            app = sess.exec(
                select(Application).where(Application.job_id == j.id)
            ).first()

            rows.append({
                "job_title": _clean(j.title),
                "company": _clean(j.company_name),
                "company_linkedin": _clean(comp.linkedin_url if comp else ""),
                "company_website": _clean(comp.website if comp else ""),
                "location": _clean(j.location),
                "remote_type": _clean(j.remote_type),
                "eligible_from_pakistan": j.eligible,
                "eligibility_reason": _clean(j.eligibility_reason),
                "salary": _clean(j.salary),
                "posted_date": j.posted_at.strftime("%Y-%m-%d") if j.posted_at else "",
                "source": j.source,
                "application_link": j.apply_url,
                "contact_name": _clean(contact.name if contact else ""),
                "contact_title": _clean(contact.title if contact else ""),
                "contact_email": _clean(contact.email if contact else ""),
                "email_confidence": contact.email_confidence if contact else "none",
                "contact_linkedin": _clean(contact.linkedin_url if contact else ""),
                "cold_email_draft": _clean(app.cold_email if app else ""),
                "match_score": j.match_score,
                "score_reason": _clean(j.score_reason),
                "tailored_resume_path": _clean(app.resume_path if app else ""),
                "status": j.status,
                "applied_date": (app.submitted_at.strftime("%Y-%m-%d")
                                 if app and app.submitted_at else ""),
                "notes": _clean(app.notes if app else ""),
            })

    # utf-8-sig so Excel on Windows renders non-ASCII company names correctly.
    with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    return dest
