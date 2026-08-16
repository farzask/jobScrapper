"""Unattended daily run: discover -> enrich -> export CSV.

Registered with Windows Task Scheduler by scripts/register_task.ps1.
Writes a dated CSV and appends a one-line summary to data/output/sweep.log.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load                      # noqa: E402
from app.enrich.runner import enrich_all         # noqa: E402
from app.export import export_csv                # noqa: E402
from app.models import Job, init_db, session     # noqa: E402
from app.pipeline import run_discovery           # noqa: E402
from sqlmodel import select                      # noqa: E402


async def sweep() -> dict:
    init_db()
    cfg = load()

    disc = await run_discovery(cfg)
    enr = await enrich_all()

    out_dir = cfg.path("output_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = export_csv(out_dir / f"jobs_{datetime.now():%Y-%m-%d}.csv", limit=500)

    with session() as sess:
        ms = cfg.get("search.min_score", 40)
        matches = len(list(sess.exec(select(Job).where(Job.match_score >= ms))))

    return {"new": disc["new"], "raw": disc["raw"], "matches": matches,
            "contacts": enr["with_contact"], "csv": str(csv_path)}


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_path = load().path("output_dir") / "sweep.log"
    try:
        res = asyncio.run(sweep())
        line = (f"{datetime.now():%Y-%m-%d %H:%M}  OK  new={res['new']} "
                f"scanned={res['raw']} matches={res['matches']} "
                f"contacts={res['contacts']}  -> {Path(res['csv']).name}")
    except Exception as exc:  # noqa: BLE001
        line = f"{datetime.now():%Y-%m-%d %H:%M}  FAIL  {type(exc).__name__}: {exc}"
        print(line)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        raise

    print(line)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


if __name__ == "__main__":
    main()
