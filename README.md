# JobApplier

Local job-hunting automation for **Pakistan-based onsite + remote-anywhere** roles.
Finds jobs across many sources, filters out the ones you can't actually hold from
Pakistan, enriches each with company LinkedIn / apply link / contact, tracks status,
and exports a CSV.

Everything runs on your machine. Nothing is uploaded anywhere.

---

## Quick start

```bash
python run.py
```

Opens <http://localhost:8000>. Click **Find jobs**, then **Download CSV**.

If port 8000 is already taken, `run.py` sorts it out rather than crashing: if the
holder is another JobApplier window it just opens that one, and if it's an
unrelated program it moves to the next free port. Force a specific port with
`python run.py --port 8123`.

Command line equivalents:

```bash
.venv/Scripts/python -m app.pipeline --discover --export   # find + export
.venv/Scripts/python -m app.enrich.runner                  # contacts + LinkedIn
python scripts/daily_sweep.py                              # the unattended run
```

## Daily automation

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

Runs every morning at 8:00. Because `-StartWhenAvailable` is set, a sleeping
laptop still gets its sweep once it wakes rather than skipping the day.
Summary lines land in `data/output/sweep.log`.

## Skills — `/skills` in the dashboard

Add your skills, mark the important ones **core**, and jobs are scored and
filtered against them. Stored in `data/skills.yaml` (kept separate from
`config.yaml` so the UI can rewrite it without destroying that file's comments).

- **Core skills** are worth roughly double when scoring.
- **Minimum skills a job must match** — `0` is off; `2–3` cuts jobs that only
  brush past your stack.
- **Must mention a core skill** — the strongest filter.
- Each skill shows a **count** of how many jobs mention it, so you can see which
  of your skills this market actually asks for.
- **Rescore** re-ranks the existing database in seconds. No re-scraping.

> ⚠️ **Skill filters are harsher than they look right now.** Only ~62% of stored
> jobs carry a description — the LinkedIn guest endpoint and Mustakbil return
> titles only, so for those jobs skill matching sees the title alone and finds
> almost nothing. With `min_matches=2`, 259 of 287 jobs were hidden, mostly for
> that reason rather than genuine mismatch. Leave the filter at `0` until
> per-job description fetching is added, and use it as a sort signal instead.

## Tuning — `config.yaml`

The single file that steers everything. The two settings that matter most:

| Setting | Effect |
|---|---|
| `search.titles` | The roles you want. Matching requires a shared *discriminating* word, so "Store Manager" will never match "project manager". |
| `search.min_score` | Threshold to reach the queue. Lower it to ~30 for more volume, raise to ~55 for only strong matches. |
| `search.exclude_titles` | Currently tuned for 0–2 years: drops senior/lead/staff/principal. |
| `search.max_years_required` | Roles demanding more than this are penalised 25 points. |

Add companies to `seeds/companies.yaml` — the prober finds their ATS board
automatically and every open role there becomes searchable.

---

## How it works

```
discover → normalize/dedupe → eligibility → filter → score → enrich → CSV
```

**Sources** (all free, no API keys):

| Tier | Sources |
|---|---|
| Remote feeds | RemoteOK, Remotive, Arbeitnow, Himalayas, Jobicy |
| ATS boards | Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee |
| Pakistan | LinkedIn public guest endpoint, Mustakbil |

The **ATS tier is where the coverage comes from** — ~4,000 of the ~4,800 jobs
scanned per run. Those postings mostly never reach an aggregator, which is
exactly the "jobs that get missed" problem. Slugs are discovered once by
`ats_prober` and cached to the DB, so coverage compounds instead of restarting
cold each night.

### Pakistan eligibility — the most important filter

Most "remote" jobs are quietly restricted to one country. Every job resolves to
**yes / no / unknown**, and `no` is dropped so you never waste an application.
A typical run drops ~200 jobs here.

Deliberate calls in `app/normalize.py`:

- **EMEA / MENA → `unknown`, not `yes`.** Pakistan is South Asia and sits
  outside both, but some companies' EMEA org does hire there. Too ambiguous to
  decide for you.
- **India → `no`.** A role restricted to India is not open to a Pakistani applicant.
- **Explicit allowlists are decisive.** When a feed gives a country list and
  Pakistan isn't on it, that's `no`, not `unknown`.
- **Title hints override vague locations.** `Backend Engineer (Europe/UK timezone)`
  is caught even when the location field just says "Remote".

### Enrichment, honestly

Search-engine scraping was tested (DuckDuckGo, Bing, Mojeek, Startpage) and all
of them now block or return unusable markup, so nothing depends on it. Instead:

- **Domain** — Clearbit's keyless autocomplete, with a strict ≥90 similarity
  gate. Loose matching resolved "Stone" to `stonetoss.com` (a webcomic), so the
  rule is now: return nothing rather than the wrong company.
- **Company LinkedIn** — harvested from LinkedIn job cards where available
  (authoritative), otherwise a constructed deep link.
- **Contact** — role inboxes (`careers@`, `hr@`) confirmed by a DNS MX lookup,
  plus a LinkedIn people-search deep link to find the actual human.

Typical coverage: **100% company LinkedIn, ~75% domain, ~55% contact email.**
Named personal emails need a paid provider — set `HUNTER_API_KEY` and
`hunter_optional()` in `app/enrich/company.py` switches on automatically
(free tier: 25 lookups/month).

---

## Project layout

```
run.py                    dashboard entrypoint
config.yaml               all tuning
seeds/companies.yaml      companies to sweep
app/
  main.py                 FastAPI routes
  models.py               SQLite schema
  pipeline.py             orchestration
  normalize.py            dedupe, eligibility, scoring
  export.py               CSV
  sources/                one adapter per source
  enrich/                 domain, LinkedIn, contacts
  templates/, static/     dashboard UI (Tailwind vendored, works offline)
scripts/daily_sweep.py    unattended run
data/output/              CSVs + sweep.log
```

## Not built yet

- **Resume tailoring** (`app/tailor/`) — needs your resume at
  `data/master_resume.docx` first. Design is settled: `python-docx` edits your
  real file so formatting is preserved, and a validator hard-blocks any employer,
  date, credential, or skill not present in `data/profile.yaml`. Tailoring may
  reorder and rephrase; it may never invent.
- **Assisted apply** (`app/apply/`) — Playwright fills each form, screenshots it,
  and waits for your one-click approval. `apply.dry_run: true` in config until
  you explicitly turn it off.
- **Rozee.pk / Indeed PK** — both return HTTP 403 (Cloudflare), so they need the
  Playwright browser path rather than plain HTTP.
