"""SQLite schema. Companies/jobs/contacts/applications/runs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine

from app.config import load


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Company(SQLModel, table=True):
    __tablename__ = "companies"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    slug: str = Field(index=True, unique=True)
    domain: Optional[str] = None
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    careers_url: Optional[str] = None
    ats_type: Optional[str] = None          # greenhouse | lever | ashby | ...
    ats_slug: Optional[str] = None
    email_pattern: Optional[str] = None     # e.g. "{first}.{last}"
    enriched_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    id: Optional[int] = Field(default=None, primary_key=True)
    dedupe_hash: str = Field(index=True, unique=True)
    title: str
    company_id: Optional[int] = Field(default=None, foreign_key="companies.id", index=True)
    company_name: str = Field(index=True)
    location: Optional[str] = None
    remote_type: Optional[str] = None       # remote | onsite | hybrid | unknown
    # Three-state Pakistan eligibility: yes / no / unknown
    eligible: str = Field(default="unknown", index=True)
    eligibility_reason: Optional[str] = None
    market: Optional[str] = None            # remote_global | pakistan_onsite
    salary: Optional[str] = None
    description: Optional[str] = None
    apply_url: str = ""
    source: str = Field(index=True)
    source_id: Optional[str] = None
    posted_at: Optional[datetime] = None
    match_score: int = Field(default=0, index=True)
    score_reason: Optional[str] = None
    status: str = Field(default="discovered", index=True)
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    __tablename__ = "contacts"
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    email_confidence: str = Field(default="none")   # verified | pattern | guess | none
    linkedin_url: Optional[str] = None
    source: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Application(SQLModel, table=True):
    __tablename__ = "applications"
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    status: str = Field(default="ready", index=True)
    resume_path: Optional[str] = None
    cover_path: Optional[str] = None
    cold_email: Optional[str] = None
    answers_json: Optional[str] = None
    screenshot_path: Optional[str] = None
    ats_type: Optional[str] = None
    submitted_at: Optional[datetime] = None
    response_at: Optional[datetime] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Run(SQLModel, table=True):
    __tablename__ = "runs"
    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    raw_count: int = 0
    new_count: int = 0
    kept_count: int = 0
    source_stats: Optional[str] = None
    error: Optional[str] = None


_engine = None


def engine():
    global _engine
    if _engine is None:
        db = load().path("db")
        db.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{db}", echo=False)
    return _engine


def init_db() -> None:
    SQLModel.metadata.create_all(engine())


def session() -> Session:
    return Session(engine())
