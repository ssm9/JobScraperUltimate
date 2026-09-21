"""JobScraperUltimate web app: REST API + static UI."""

import csv
import io
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import DEFAULT_SETTINGS, get_conn, get_settings, init_db, save_settings
from .mailer import send_test_email
from .scraper import JOBSPY_SITES, company_is_excluded
from .worker import worker

logging.basicConfig(
    level=os.environ.get("JSU_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("jsu")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
VALID_STATUSES = ("new", "saved", "applied", "hidden")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    worker.start()
    log.info("JobScraperUltimate started")
    yield
    worker.stop()


app = FastAPI(title="JobScraperUltimate", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- models


class JobUpdate(BaseModel):
    status: str | None = None
    notes: str | None = None


class BulkUpdate(BaseModel):
    ids: list[int] = Field(default_factory=list)
    status: str


class BlockCompany(BaseModel):
    company: str
    hide_existing: bool = True


class SettingsUpdate(BaseModel):
    model_config = {"extra": "ignore"}

    search_term: str | None = None
    location: str | None = None
    country_to_search: str | None = None
    scrape_from: list[str] | None = None
    results_fetch_count: int | None = Field(default=None, ge=1, le=1000)
    hours: int | None = Field(default=None, ge=1, le=720)
    job_type: str | None = None
    is_remote: bool | None = None
    fetch_description: bool | None = None
    roles_of_interest: list[str] | None = None
    exclude_keywords: list[str] | None = None
    exclude_companies: list[str] | None = None
    collapse_duplicates: bool | None = None
    schedule_enabled: bool | None = None
    sleep_time: int | None = Field(default=None, ge=60, le=86400)
    email_send: bool | None = None
    from_email: str | None = None
    email_password: str | None = None
    to_email: str | None = None
    email_smtp: str | None = None
    email_port: int | None = Field(default=None, ge=1, le=65535)


# --------------------------------------------------------------------------- jobs


@app.get("/api/jobs")
def list_jobs(
    q: str = "",
    status: str = "",
    site: str = "",
    company: str = "",
    sort: str = "first_seen",
    order: str = "desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
):
    where, params = [], []
    if q:
        where.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
        params += [f"%{q}%"] * 3
    if status:
        statuses = [s for s in status.split(",") if s in VALID_STATUSES]
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params += statuses
    else:
        where.append("status != 'hidden'")  # hidden jobs are opt-in
    if site:
        where.append("site = ?")
        params.append(site)
    if company:
        where.append("company = ?")
        params.append(company)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sort_col = sort if sort in ("first_seen", "title", "company", "location", "site") else "first_seen"
    direction = "ASC" if order.lower() == "asc" else "DESC"

    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM jobs {clause}", params).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT id, job_url, title, company, location, site, job_type, is_remote,
                       salary, date_posted, first_seen, status, notes
                FROM jobs {clause} ORDER BY {sort_col} {direction}, id DESC
                LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

    return {
        "jobs": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return dict(row)


@app.patch("/api/jobs/{job_id}")
def update_job(job_id: int, payload: JobUpdate):
    fields, params = [], []
    if payload.status is not None:
        if payload.status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {VALID_STATUSES}")
        fields.append("status = ?")
        params.append(payload.status)
    if payload.notes is not None:
        fields.append("notes = ?")
        params.append(payload.notes)
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")

    with get_conn() as conn:
        cur = conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", params + [job_id])
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


@app.post("/api/jobs/bulk")
def bulk_update(payload: BulkUpdate):
    if payload.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {VALID_STATUSES}")
    if not payload.ids:
        return {"updated": 0}
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE jobs SET status = ? WHERE id IN ({','.join('?' * len(payload.ids))})",
            [payload.status] + payload.ids,
        )
    return {"updated": cur.rowcount}


@app.delete("/api/jobs")
def delete_jobs(status: str = "", older_than_days: int = 0):
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if older_than_days > 0:
        where.append("first_seen < datetime('now', ?)")
        params.append(f"-{older_than_days} days")
    if not where:
        raise HTTPException(status_code=400, detail="Specify status or older_than_days")
    with get_conn() as conn:
        cur = conn.execute(f"DELETE FROM jobs WHERE {' AND '.join(where)}", params)
    return {"deleted": cur.rowcount}


@app.get("/api/export.csv")
def export_csv(status: str = "", q: str = ""):
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("(title LIKE ? OR company LIKE ?)")
        params += [f"%{q}%"] * 2
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT title, company, location, site, job_type, salary, date_posted,
                       first_seen, status, job_url FROM jobs {clause}
                ORDER BY first_seen DESC""",
            params,
        ).fetchall()

    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_NONNUMERIC)
    writer.writerow(["title", "company", "location", "site", "job_type", "salary",
                     "date_posted", "first_seen", "status", "job_url"])
    writer.writerows([tuple(row) for row in rows])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="jobs_export.csv"'},
    )


@app.get("/api/companies")
def list_companies(limit: int = Query(50, ge=1, le=500)):
    """Companies by posting count — the top of this list is usually the spam."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT company,
                      COUNT(*) AS total,
                      COUNT(DISTINCT fingerprint) AS distinct_roles,
                      SUM(CASE WHEN status = 'hidden' THEN 1 ELSE 0 END) AS hidden
               FROM jobs WHERE company != ''
               GROUP BY company ORDER BY total DESC, company ASC LIMIT ?""",
            (limit,),
        ).fetchall()

    blocked = get_settings().get("exclude_companies", [])
    return {
        "companies": [
            dict(row, blocked=company_is_excluded(row["company"], blocked)) for row in rows
        ]
    }


@app.post("/api/companies/block")
def block_company(payload: BlockCompany):
    """Add a company to the blocklist and optionally hide what it already posted."""
    name = payload.company.strip()
    if not name:
        raise HTTPException(status_code=400, detail="company is required")

    settings = get_settings()
    blocked = list(settings.get("exclude_companies", []))
    if not company_is_excluded(name, blocked):
        blocked.append(name)
        save_settings({"exclude_companies": blocked})

    hidden = 0
    if payload.hide_existing:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'hidden' WHERE company = ? AND status != 'hidden'",
                (name,),
            )
            hidden = cur.rowcount

    return {"ok": True, "company": name, "hidden": hidden, "exclude_companies": blocked}


@app.post("/api/companies/unblock")
def unblock_company(payload: BlockCompany):
    """Remove a company from the blocklist. Already-hidden jobs stay hidden."""
    name = payload.company.strip()
    blocked = get_settings().get("exclude_companies", [])
    remaining = [c for c in blocked if not company_is_excluded(name, [c])]
    save_settings({"exclude_companies": remaining})
    return {"ok": True, "exclude_companies": remaining}


@app.post("/api/jobs/collapse-duplicates")
def collapse_duplicates():
    """Hide repeat postings of a role already in the database.

    Keeps the earliest posting of each (company, role) pair and hides the rest.
    Applies to what was scraped before collapsing was switched on.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE jobs SET status = 'hidden'
               WHERE status NOT IN ('hidden', 'applied', 'saved')
                 AND fingerprint IS NOT NULL
                 AND id NOT IN (
                     SELECT MIN(id) FROM jobs WHERE fingerprint IS NOT NULL
                     GROUP BY fingerprint
                 )"""
        )
    return {"ok": True, "hidden": cur.rowcount}


# --------------------------------------------------------------------------- settings


@app.get("/api/settings")
def read_settings():
    settings = get_settings()
    settings["email_password"] = "********" if settings.get("email_password") else ""
    return {"settings": settings, "defaults": DEFAULT_SETTINGS, "available_sites": JOBSPY_SITES + ["skillsire"]}


@app.put("/api/settings")
def write_settings(payload: SettingsUpdate):
    updates = payload.model_dump(exclude_none=True)
    # The UI shows a masked placeholder; don't overwrite the stored password with it.
    if updates.get("email_password") == "********":
        updates.pop("email_password")
    for key in ("roles_of_interest", "exclude_keywords", "exclude_companies", "scrape_from"):
        if key in updates:
            updates[key] = [item.strip() for item in updates[key] if item and item.strip()]
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    settings = save_settings(updates)
    worker.reconfigure()  # pick up schedule changes without waiting out the current sleep
    settings["email_password"] = "********" if settings.get("email_password") else ""
    return {"ok": True, "settings": settings}


@app.post("/api/settings/test-email")
def test_email():
    settings = get_settings()
    missing = [k for k in ("from_email", "email_password", "to_email", "email_smtp") if not settings.get(k)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing email settings: {', '.join(missing)}")
    try:
        send_test_email(settings)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Send failed: {exc}") from exc
    return {"ok": True, "message": f"Test email sent to {settings['to_email']}"}


# --------------------------------------------------------------------------- runs & status


@app.post("/api/scrape/run")
def trigger_scrape():
    if worker.running:
        raise HTTPException(status_code=409, detail="A scrape is already running")
    if not worker.trigger():
        raise HTTPException(status_code=409, detail="A scrape is already running")
    return {"ok": True, "message": "Scrape started"}


@app.get("/api/runs")
def list_runs(limit: int = Query(25, ge=1, le=200)):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"runs": [dict(row) for row in rows]}


@app.get("/api/stats")
def stats():
    with get_conn() as conn:
        counts = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE first_seen >= datetime('now','-1 day')"
        ).fetchone()["n"]
        last_run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        sites = conn.execute(
            "SELECT site, COUNT(*) AS n FROM jobs GROUP BY site ORDER BY n DESC"
        ).fetchall()

    settings = get_settings()
    return {
        "total": total,
        "last_24h": today,
        "by_status": {row["status"]: row["n"] for row in counts},
        "by_site": {row["site"] or "unknown": row["n"] for row in sites},
        "last_run": dict(last_run) if last_run else None,
        "scraping": worker.running,
        "schedule_enabled": bool(settings.get("schedule_enabled")),
        "interval_seconds": settings.get("sleep_time"),
        "next_run_at": worker.next_run_at,
    }


@app.get("/health")
def health():
    return {"status": "ok", "scraping": worker.running}


# --------------------------------------------------------------------------- static UI


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
