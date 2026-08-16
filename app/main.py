"""FastAPI dashboard. Local-only web app at http://localhost:8000."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.config import ROOT, load
from app.export import export_csv
from app.models import Application, Company, Contact, Job, Run, init_db, session
from app.pipeline import run_discovery

log = logging.getLogger("web")

app = FastAPI(title="JobApplier")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")

# Simple in-process job state -- this is a single-user local tool, so a dict
# is the right amount of machinery.
STATE: dict = {"running": False, "task": None, "message": "idle", "last": None}

STATUSES = ["discovered", "scored", "shortlisted", "tailored", "ready",
            "submitted", "interviewing", "rejected", "ghosted", "skipped"]


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _rows(q: str = "", eligible: str = "", market: str = "",
          status: str = "", min_score: int | None = None, limit: int = 300):
    cfg = load()
    ms = cfg.get("search.min_score", 40) if min_score is None else min_score
    with session() as sess:
        stmt = select(Job).where(Job.match_score >= ms)
        if eligible:
            stmt = stmt.where(Job.eligible == eligible)
        if market:
            stmt = stmt.where(Job.market == market)
        if status:
            stmt = stmt.where(Job.status == status)
        jobs = list(sess.exec(stmt.order_by(Job.match_score.desc()).limit(limit)))

        if q:
            ql = q.lower()
            jobs = [j for j in jobs
                    if ql in j.title.lower() or ql in j.company_name.lower()]

        comps = {c.id: c for c in sess.exec(select(Company)).all()}
        contacts: dict[int, Contact] = {}
        for c in sess.exec(select(Contact)).all():
            contacts.setdefault(c.company_id, c)
        apps = {a.job_id: a for a in sess.exec(select(Application)).all()}

        out = []
        for j in jobs:
            comp = comps.get(j.company_id)
            out.append({
                "j": j,
                "company": comp,
                "contact": contacts.get(j.company_id),
                "app": apps.get(j.id),
            })
        return out


def _stats() -> dict:
    with session() as sess:
        jobs = list(sess.exec(select(Job)).all())
        cfg = load()
        ms = cfg.get("search.min_score", 40)
        scored = [j for j in jobs if j.match_score >= ms]
        last_run = sess.exec(select(Run).order_by(Run.id.desc())).first()
        return {
            "total": len(jobs),
            "scored": len(scored),
            "eligible": sum(1 for j in scored if j.eligible == "yes"),
            "pakistan": sum(1 for j in scored
                            if (j.market or "").startswith("pakistan")),
            "applied": sum(1 for j in jobs if j.status == "submitted"),
            "target": cfg.get("search.target_jobs", 30),
            "min_score": ms,
            "last_run": last_run,
        }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, q: str = "", eligible: str = "",
              market: str = "", status: str = ""):
    return templates.TemplateResponse(request, "dashboard.html", {
        "rows": _rows(q=q, eligible=eligible, market=market, status=status),
        "stats": _stats(),
        "filters": {"q": q, "eligible": eligible, "market": market, "status": status},
        "statuses": STATUSES,
        "state": STATE,
    })


@app.get("/api/state")
def api_state():
    return JSONResponse({"running": STATE["running"], "message": STATE["message"],
                         "last": STATE["last"]})


async def _do_run(with_enrich: bool) -> None:
    from app.enrich.runner import enrich_all
    STATE.update(running=True, message="Searching job sources...")
    try:
        res = await run_discovery()
        STATE["message"] = (f"Found {res['new']} new jobs "
                            f"({res['raw']} scanned). Enriching...")
        if with_enrich:
            e = await enrich_all()
            STATE["message"] = (f"Done. {res['new']} new jobs, "
                                f"{e['with_contact']} contacts found.")
        else:
            STATE["message"] = f"Done. {res['new']} new jobs."
        STATE["last"] = res
    except Exception as exc:  # noqa: BLE001
        log.exception("run failed")
        STATE["message"] = f"Run failed: {type(exc).__name__}: {exc}"
    finally:
        STATE["running"] = False


@app.post("/run")
async def trigger_run(enrich: str = Form("1")):
    if STATE["running"]:
        return JSONResponse({"ok": False, "message": "already running"}, 409)

    # Flip the flag here, not inside _do_run: the task starts on the next event
    # loop tick, so a fast double-click would otherwise launch two runs.
    STATE.update(running=True, message="Starting...")

    # Scheduled on the loop rather than handed to BackgroundTasks, so the run
    # is independent of this request's lifecycle and the response returns
    # immediately for the UI to poll. Keep a reference so it isn't GC'd.
    STATE["task"] = asyncio.create_task(_do_run(enrich == "1"))
    return JSONResponse({"ok": True})


@app.post("/jobs/{job_id}/status")
def set_status(job_id: int, status: str = Form(...)):
    with session() as sess:
        j = sess.get(Job, job_id)
        if not j:
            return JSONResponse({"ok": False}, 404)
        j.status = status
        sess.add(j)
        sess.commit()
    return JSONResponse({"ok": True, "status": status})


@app.get("/skills", response_class=HTMLResponse)
def skills_page(request: Request, msg: str = ""):
    from app.skills import load_skills
    profile = load_skills()
    with session() as sess:
        jobs = list(sess.exec(select(Job)).all())
    # How often each skill actually appears, so you can see which of your
    # skills are pulling their weight in this market.
    counts: dict[str, int] = {s.name: 0 for s in profile.skills}
    for j in jobs:
        for m in (j.matched_skills or "").split(", "):
            if m in counts:
                counts[m] += 1
    return templates.TemplateResponse(request, "skills.html", {
        "profile": profile, "counts": counts,
        "total_jobs": len(jobs), "msg": msg, "state": STATE,
    })


@app.post("/skills/add")
def skills_add(name: str = Form(...), core: str = Form("")):
    from app.skills import Skill, load_skills, save_skills
    profile = load_skills()
    for raw in name.replace("\n", ",").split(","):
        clean = raw.strip().lower()
        if clean and clean not in profile.names():
            profile.skills.append(Skill(clean, core == "1"))
    save_skills(profile)
    return RedirectResponse("/skills?msg=Skill+added.+Rescore+to+apply.", 303)


@app.post("/skills/remove")
def skills_remove(name: str = Form(...)):
    from app.skills import load_skills, save_skills
    profile = load_skills()
    profile.skills = [s for s in profile.skills if s.name != name.strip().lower()]
    save_skills(profile)
    return RedirectResponse("/skills?msg=Skill+removed.+Rescore+to+apply.", 303)


@app.post("/skills/toggle")
def skills_toggle(name: str = Form(...)):
    from app.skills import load_skills, save_skills
    profile = load_skills()
    for s in profile.skills:
        if s.name == name.strip().lower():
            s.core = not s.core
    save_skills(profile)
    return RedirectResponse("/skills?msg=Updated.+Rescore+to+apply.", 303)


@app.post("/skills/settings")
def skills_settings(min_matches: int = Form(0), require_core: str = Form("")):
    from app.skills import load_skills, save_skills
    profile = load_skills()
    profile.min_matches = max(0, min_matches)
    profile.require_core = require_core == "1"
    save_skills(profile)
    return RedirectResponse("/skills?msg=Filter+updated.+Rescore+to+apply.", 303)


@app.post("/rescore")
def rescore():
    from app.pipeline import rescore_all
    res = rescore_all()
    return RedirectResponse(
        f"/skills?msg=Rescored+{res['total']}+jobs+"
        f"({res['changed']}+changed,+{res['hidden']}+filtered+out)", 303)


@app.get("/export.csv")
def download_csv():
    path = export_csv(limit=500)
    return FileResponse(path, media_type="text/csv", filename=Path(path).name)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    with session() as sess:
        j = sess.get(Job, job_id)
        if not j:
            return HTMLResponse("Not found", 404)
        comp = sess.get(Company, j.company_id) if j.company_id else None
        contact = sess.exec(
            select(Contact).where(Contact.company_id == j.company_id)).first()
        app_row = sess.exec(
            select(Application).where(Application.job_id == job_id)).first()
    return templates.TemplateResponse(request, "detail.html", {
        "j": j, "company": comp,
        "contact": contact, "app": app_row, "statuses": STATUSES,
    })
