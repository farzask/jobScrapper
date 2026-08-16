"""Company + contact enrichment on free tiers only.

Deliberate design note: search-engine scraping (DuckDuckGo/Bing/Mojeek) was
tested and all of them now block or return unusable markup, so nothing here
depends on it. We use deterministic sources instead:

  * Clearbit's keyless autocomplete API for name -> domain
  * LinkedIn company URLs harvested straight from LinkedIn job cards
  * DNS MX records to prove a domain actually accepts mail
  * Constructed LinkedIn people-search deep links for the human step

What this honestly delivers: a verified domain and company LinkedIn for most
companies, deliverable role-based emails (careers@/hr@) where MX confirms it,
and a one-click people-search link to find the actual hiring manager. Named
personal emails need a paid provider (Hunter/Apollo) -- see hunter_optional().
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote_plus

import httpx

log = logging.getLogger(__name__)

CLEARBIT = "https://autocomplete.clearbit.com/v1/companies/suggest"

# Mailboxes worth trying, most likely to be read by a hiring human first.
ROLE_MAILBOXES = ["careers", "jobs", "hr", "recruitment", "hiring", "info"]

_LEGAL = re.compile(r"\b(inc|llc|ltd|limited|gmbh|corp|corporation|plc|bv|ag|"
                    r"pvt|private|technologies|technology|solutions|systems|"
                    r"software|labs|group|holdings|co)\b\.?", re.I)


def linkedin_slug(name: str) -> str:
    s = _LEGAL.sub(" ", (name or "").lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)


def linkedin_company_url(name: str) -> str:
    """Constructed deep link. Correct for most companies; unverified."""
    return f"https://www.linkedin.com/company/{linkedin_slug(name)}"


def people_search_url(company: str, role: str = "recruiter OR talent OR hiring") -> str:
    """LinkedIn people search scoped to a company.

    This is the realistic free path to a named hiring manager: the tool takes
    you to the right result set, you pick the human.
    """
    return ("https://www.linkedin.com/search/results/people/?keywords="
            + quote_plus(f'{company} ({role})'))


async def resolve_domain(client: httpx.AsyncClient, name: str) -> tuple[str | None, str | None]:
    """Company name -> (domain, canonical_name) via Clearbit autocomplete."""
    try:
        r = await client.get(CLEARBIT, params={"query": name}, timeout=20)
        if r.status_code != 200:
            return None, None
        results = r.json() or []
    except Exception as exc:  # noqa: BLE001
        log.debug("clearbit failed for %s: %s", name, exc)
        return None, None
    if not results:
        return None, None

    # Clearbit is a fuzzy autocomplete, so it happily returns a *different*
    # company with a similar name -- "Stone" resolves to stonetoss.com (a
    # webcomic) and "IKONIC" to ikonick.com. Substring containment is far too
    # permissive here. Require a strong exact-ish match and otherwise return
    # nothing: a missing domain is much cheaper than emailing the wrong
    # company's careers inbox.
    from rapidfuzz import fuzz as _fuzz

    def key(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    want = key(name)
    if not want:
        return None, None

    best, best_score = None, 0.0
    for r_ in results:
        got = key(r_.get("name"))
        dom_root = key((r_.get("domain") or "").split(".")[0])
        score = max(_fuzz.ratio(want, got), _fuzz.ratio(want, dom_root))
        if got == want or dom_root == want:
            score = 100.0
        if score > best_score:
            best, best_score = r_, score

    if best is None or best_score < 90:
        log.debug("no confident domain for %r (best=%.0f)", name, best_score)
        return None, None
    return best.get("domain"), best.get("name")


def has_mx(domain: str) -> bool:
    """True if the domain publishes MX records, i.e. it can receive mail."""
    if not domain:
        return False
    try:
        import dns.resolver
        res = dns.resolver.Resolver()
        res.lifetime = res.timeout = 5.0
        return bool(res.resolve(domain, "MX"))
    except Exception:  # noqa: BLE001 - any DNS failure means "can't confirm"
        return False


def role_emails(domain: str) -> list[str]:
    return [f"{m}@{domain}" for m in ROLE_MAILBOXES]


def person_email_patterns(first: str, last: str, domain: str) -> list[str]:
    f, l = first.lower(), last.lower()
    return [f"{f}.{l}@{domain}", f"{f}{l}@{domain}", f"{f[0]}{l}@{domain}",
            f"{f}@{domain}", f"{f}_{l}@{domain}", f"{f[0]}.{l}@{domain}"]


async def enrich_one(client: httpx.AsyncClient, name: str,
                     linkedin_hint: str | None = None) -> dict:
    """Everything we can learn about one company for free."""
    domain, canonical = await resolve_domain(client, name)

    mx = await asyncio.to_thread(has_mx, domain) if domain else False
    emails = role_emails(domain) if (domain and mx) else []

    return {
        "name": canonical or name,
        "domain": domain,
        "website": f"https://{domain}" if domain else None,
        # A harvested LinkedIn URL is authoritative; a constructed one is a
        # best guess. Track which so the CSV can be honest about it.
        "linkedin_url": linkedin_hint or linkedin_company_url(name),
        "linkedin_verified": bool(linkedin_hint),
        "mx_ok": mx,
        "contact_email": emails[0] if emails else None,
        "email_confidence": "verified-domain" if mx else "none",
        "alt_emails": emails[1:4],
        "people_search": people_search_url(canonical or name),
    }


def hunter_optional(domain: str, api_key: str | None) -> dict | None:
    """Paid upgrade path, off by default.

    Hunter's free tier (25 searches/month) returns real named contacts with
    verified personal emails. Set HUNTER_API_KEY to switch this on.
    """
    if not (api_key and domain):
        return None
    try:
        r = httpx.get("https://api.hunter.io/v2/domain-search",
                      params={"domain": domain, "api_key": api_key, "limit": 5},
                      timeout=25)
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data") or {}
        people = data.get("emails") or []
        best = next((p for p in people
                     if any(k in (p.get("position") or "").lower()
                            for k in ("recruit", "talent", "hr", "people", "hiring"))),
                    people[0] if people else None)
        if not best:
            return None
        return {
            "name": " ".join(filter(None, [best.get("first_name"), best.get("last_name")])),
            "title": best.get("position"),
            "email": best.get("value"),
            "email_confidence": "verified" if best.get("confidence", 0) > 70 else "pattern",
            "linkedin_url": best.get("linkedin"),
            "pattern": data.get("pattern"),
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("hunter lookup failed: %s", exc)
        return None
