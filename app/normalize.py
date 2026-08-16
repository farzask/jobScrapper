"""Normalize, dedupe, classify eligibility, and score.

The eligibility classifier is the highest-value piece here. Most "remote"
postings are quietly restricted to one country; applying to those from
Pakistan is wasted effort. We resolve every job to yes / no / unknown and
never silently discard the unknowns.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

from rapidfuzz import fuzz

from app.sources.base import RawJob

# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------

# Phrases that mean "anyone, anywhere" -> eligible.
_WORLDWIDE = re.compile(
    r"\b(worldwide|world wide|anywhere in the world|anywhere|global(?:ly)?|"
    r"any location|any country|international|fully remote|remote - global|"
    r"no location restriction|location independent)\b", re.I)

# Regions that genuinely include Pakistan -> eligible.
# Deliberately NOT here: EMEA and MENA (Europe/Middle East/Africa -- Pakistan
# is South Asia and falls outside both), and India (a role restricted to India
# is not open to a Pakistani applicant). Getting this wrong produces false
# "eligible" results, which is the costly direction: a wasted application.
_INCLUDES_PK = re.compile(
    r"\b(pakistan|south asia|asia(?:\s*[-/]?\s*pacific)?|apac|"
    r"karachi|lahore|islamabad)\b", re.I)

# Business-region shorthand that *sometimes* extends to Pakistan in practice
# but does not geographically. Too ambiguous to call either way -> unknown.
_AMBIGUOUS = re.compile(r"\b(emea|mena|middle east|eastern hemisphere)\b", re.I)

# Regions that exclude Pakistan. Only decisive when nothing above matched.
_EXCLUSIVE = re.compile(
    r"\b(u\.?s\.?a?\.?|united states|us[- ]only|us based|usa only|canada|canadian|"
    r"north america|latam|latin america|south america|brazil|mexico|argentina|"
    r"europe|european|eu only|eea|uk|united kingdom|england|ireland|germany|"
    r"france|spain|portugal|poland|netherlands|australia|new zealand|india|"
    r"philippines|japan|singapore|nigeria|kenya|south africa)\b", re.I)

# Location/timezone hints that appear in the TITLE, e.g.
# "Backend Engineer (Europe/UK timezone)". Narrow on purpose -- matching the
# whole title would misread "Engineer, US Payments" as a US-only role.
_TITLE_HINT = re.compile(
    r"\(([^)]*(?:timezone|time zone|remote|based|only|hours)[^)]*)\)", re.I)

_PK_CITIES = re.compile(
    r"\b(pakistan|karachi|lahore|islamabad|rawalpindi|peshawar|faisalabad|"
    r"multan|quetta|sialkot|gujranwala)\b", re.I)


def classify_eligibility(job: RawJob) -> tuple[str, str]:
    """Return (yes|no|unknown, human-readable reason).

    Order matters: an explicit worldwide/Pakistan/Asia signal always beats a
    country list, because postings routinely read "Worldwide (US, UK, PK)".
    """
    parts = [job.location_restriction, job.location, job.remote_type]
    # A parenthetical region hint in the title overrides a vaguer location
    # field -- "Remote (EMEA)" as location plus "(Europe/UK timezone)" in the
    # title means the title is telling you the truth.
    hint = _TITLE_HINT.search(job.title or "")
    if hint:
        parts.insert(0, hint.group(1))
    text = " ".join(filter(None, parts)).strip()

    if _PK_CITIES.search(text or ""):
        return "yes", "Pakistan-based role or Pakistan explicitly included"

    if not text:
        return "unknown", "no location information given"

    # Checked before the worldwide test: "Worldwide (EMEA)" is not worldwide.
    if _AMBIGUOUS.search(text):
        return "unknown", f"ambiguous business region, verify manually ({text[:55]})"

    if _WORLDWIDE.search(text):
        return "yes", f"open worldwide ({text[:60]})"

    if _INCLUDES_PK.search(text):
        return "yes", f"region includes Pakistan ({text[:60]})"

    if _EXCLUSIVE.search(text):
        return "no", f"restricted to another region ({text[:60]})"

    # The feed handed us a definitive allowlist and Pakistan is not on it.
    # That is a "no", not an "unknown" -- treating it as unknown is how you
    # end up applying to jobs you are not permitted to hold.
    if job.restriction_explicit:
        return "no", f"allowlist excludes Pakistan ({text[:60]})"

    if re.fullmatch(r"\s*remote\s*", text, re.I):
        return "unknown", "listed as remote with no stated restriction"

    return "unknown", f"could not classify ({text[:60]})"


_REMOTE_HINT = re.compile(r"\b(remote|anywhere|worldwide|work from home|wfh|"
                          r"distributed|virtual)\b", re.I)
_HYBRID_HINT = re.compile(r"\bhybrid\b", re.I)


def infer_remote_type(job: RawJob) -> str:
    """Fill in remote_type when a source doesn't state it.

    ATS boards almost never expose a remote flag -- Greenhouse encodes it in
    the location string ("Remote, United States"). Without this, every remote
    ATS role looks onsite and gets dropped, which silently throws away the
    highest-quality tier we have.
    """
    if job.remote_type:
        return job.remote_type
    blob = " ".join(filter(None, [job.location, job.location_restriction])) or ""
    if _HYBRID_HINT.search(blob):
        return "hybrid"
    if _REMOTE_HINT.search(blob):
        return "remote"
    return "onsite"


def detect_market(job: RawJob, eligible: str = "unknown") -> str:
    """Which of the two markets this job belongs to.

    You want Pakistan-based roles (any arrangement) or genuinely global
    remote roles. An onsite role in Berlin is neither.
    """
    blob = " ".join(filter(None, [job.location, job.location_restriction])) or ""
    is_remote = infer_remote_type(job) == "remote"
    if _PK_CITIES.search(blob):
        return "pakistan_remote" if is_remote else "pakistan_onsite"
    if is_remote:
        return "remote_global"
    return "other"


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------

_NOISE = re.compile(r"\b(senior|sr\.?|junior|jr\.?|lead|principal|staff|"
                    r"remote|hybrid|onsite|full[- ]time|part[- ]time|"
                    r"contract|permanent|new|urgent|hiring|m/f/d|x/f/m|"
                    r"\(.*?\)|\[.*?\])\b", re.I)


def norm_text(s: str) -> str:
    s = (s or "").lower()
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def company_slug(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"\b(inc|llc|ltd|limited|gmbh|corp|corporation|co|plc|bv|ag|"
               r"pvt|private|technologies|technology|labs|group|holdings)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-+", "-", s) or "unknown"


def canonical_url(url: str) -> str:
    """Strip tracking params so the same job on two feeds collapses."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))
    except ValueError:
        return url


def dedupe_hash(job: RawJob) -> str:
    key = f"{company_slug(job.company_name)}|{norm_text(job.title)}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def dedupe(jobs: list[RawJob]) -> list[RawJob]:
    """Collapse the same posting appearing across multiple feeds.

    Exact hash first (cheap), then a fuzzy pass within each company to catch
    title variants that survive normalization.
    """
    by_hash: dict[str, RawJob] = {}
    for j in jobs:
        h = dedupe_hash(j)
        prev = by_hash.get(h)
        # Prefer the record with a real description and an apply URL.
        if prev is None or (len(j.description or "") > len(prev.description or "")):
            by_hash[h] = j

    kept: list[RawJob] = []
    seen_by_company: dict[str, list[str]] = {}
    for j in by_hash.values():
        slug = company_slug(j.company_name)
        title = norm_text(j.title)
        if any(fuzz.ratio(title, t) > 92 for t in seen_by_company.get(slug, [])):
            continue
        seen_by_company.setdefault(slug, []).append(title)
        kept.append(j)
    return kept


# --------------------------------------------------------------------------
# Filtering + scoring
# --------------------------------------------------------------------------

def passes_hard_filters(job: RawJob, cfg) -> tuple[bool, str]:
    title_l = (job.title or "").lower()

    for bad in cfg.get("search.exclude_titles", []):
        if bad.lower().strip() in title_l:
            return False, f"excluded title keyword '{bad}'"

    max_age = cfg.get("search.max_age_days", 30)
    if job.posted_at:
        age = datetime.now(timezone.utc) - job.posted_at
        if age > timedelta(days=max_age):
            return False, f"older than {max_age} days"

    if not job.apply_url:
        return False, "no apply URL"

    # You are in Pakistan and want either a Pakistan-based role or a genuinely
    # remote one. An onsite role in another country is unreachable, so drop it
    # rather than letting it dilute the queue.
    if detect_market(job) == "other":
        return False, "onsite role outside Pakistan"

    return True, ""


_GENERIC_TOKENS = {"senior", "junior", "lead", "the", "of", "and", "a", "an"}

# Head nouns that appear in almost every job title. Matching on one of these
# alone is meaningless -- "Store Manager" would otherwise match "project
# manager", and "Engineer Officer" would match "software engineer". A match
# only counts when it shares a *discriminating* word (flutter, mobile,
# project, frontend...), not just the head noun.
_HEAD_NOUNS = {
    "engineer", "engineering", "developer", "development", "manager",
    "management", "analyst", "coordinator", "specialist", "officer",
    "associate", "consultant", "lead", "architect", "administrator",
    "executive", "assistant", "director", "intern", "trainee", "graduate",
}


def _title_match(job_title: str, want: str) -> float:
    """0.0-1.0 similarity, gated on sharing a *discriminating* role word.

    Pure fuzzy ratios are far too generous on short strings -- "Graphic
    Designer" scores well against "Data Engineer" on partial_ratio alone.
    Requiring a shared non-generic token kills that whole class of false
    positive, including the retail-manager noise that head-noun matching lets
    through.
    """
    jt, wt = norm_text(job_title), norm_text(want)
    j_tok = {t for t in jt.split() if t not in _GENERIC_TOKENS}
    w_tok = {t for t in wt.split() if t not in _GENERIC_TOKENS}
    if not j_tok or not w_tok:
        return 0.0

    shared = j_tok & w_tok
    if not shared:
        return 0.0                      # no shared role word -> not this job

    # The wanted title's own discriminating words, e.g. {flutter} in
    # "flutter developer" or {project} in "junior project coordinator".
    w_specific = w_tok - _HEAD_NOUNS
    if w_specific and not (shared & w_specific):
        return 0.0                      # only the head noun matched -> reject

    overlap = len(shared) / len(w_tok)
    ratio = max(fuzz.token_set_ratio(wt, jt), fuzz.partial_ratio(wt, jt)) / 100.0
    return 0.5 * overlap + 0.5 * ratio


def score(job: RawJob, cfg) -> tuple[int, str]:
    """0-100 keyword/recency score. LLM re-scoring layers on top in M4."""
    reasons: list[str] = []

    best = 0.0
    best_t = ""
    for want in cfg.get("search.titles", []):
        r = _title_match(job.title, want)
        if r > best:
            best, best_t = r, want
    title_score = int(best * 55)            # up to 55 pts
    if best >= 0.7:
        reasons.append(f"title~'{best_t}' ({best:.2f})")

    body = f"{job.title} {job.description or ''}".lower()
    kws = cfg.get("search.keywords", [])
    hits = [k for k in kws if k.lower() in body]
    kw_score = min(len(hits) * 6, 25)       # up to 25 pts
    if hits:
        reasons.append(f"skills: {', '.join(hits[:6])}")

    recency = 0
    if job.posted_at:
        days = (datetime.now(timezone.utc) - job.posted_at).days
        recency = 10 if days <= 3 else 7 if days <= 7 else 4 if days <= 14 else 0
        if days <= 7:
            reasons.append(f"posted {days}d ago")

    bonus = 0
    if job.salary:
        bonus += 5
        reasons.append("salary listed")
    if (job.description or "") and len(job.description) > 400:
        bonus += 5

    exp, exp_note = _experience_fit(job, cfg)
    if exp_note:
        reasons.append(exp_note)

    total = max(0, min(title_score + kw_score + recency + bonus + exp, 100))
    return total, "; ".join(reasons) or "no strong signals"


# Matches "5+ years", "3-5 years", "minimum 4 years of experience".
_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|-|\s*to\s*\d{1,2})?\s*years?\b[^.]{0,30}"
                    r"(?:experience|exp\b)", re.I)
_ENTRY_HINT = re.compile(
    r"\b(entry[- ]level|fresh graduate|fresh grad|no experience|graduate program|"
    r"associate|junior|trainee|intern(?:ship)?|0[-– ]?2 years|1[-– ]?2 years)\b", re.I)


def _experience_fit(job: RawJob, cfg) -> tuple[int, str]:
    """Reward roles matching the target experience band, punish ones above it.

    For an entry-level search this matters as much as the title: a "Software
    Engineer" post demanding 7 years is a title match and a total mismatch.
    """
    level = cfg.get("search.experience_level", "entry")
    if level not in {"entry", "mid"}:
        return 0, ""

    blob = f"{job.title} {(job.description or '')[:2500]}"
    max_years = cfg.get("search.max_years_required", 3)

    demanded = [int(m.group(1)) for m in _YEARS.finditer(blob)]
    demanded = [d for d in demanded if d <= 25]
    if demanded:
        need = min(demanded)            # the lowest stated bar is the real one
        if need > max_years:
            return -25, f"wants {need}+ yrs experience"
        return 8, f"asks {need} yrs (fits)"

    if _ENTRY_HINT.search(blob):
        return 12, "entry-level role"
    return 0, ""
