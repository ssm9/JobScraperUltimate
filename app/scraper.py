"""Scraping logic, adapted from the original JobScraperUltimate.py script.

Same sources (jobspy for LinkedIn/Indeed/etc., plus the Skillsire API) and the
same title-keyword filtering, but results are normalised into dicts and stored
in SQLite rather than appended to a CSV.
"""

import json
import logging
import math
import re
import sqlite3

import requests

from .db import get_conn, utcnow

log = logging.getLogger("jsu.scraper")

SKILLSIRE_API = "https://www.skillsire.com/api/job/all-jobs"
SKILLSIRE_BATCH = 20

# Sites handled by the jobspy library (everything except Skillsire).
JOBSPY_SITES = ["linkedin", "indeed", "zip_recruiter", "glassdoor", "google", "bayt", "naukri"]


# Legal suffixes that make the same employer look like several companies.
_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|gmbh|plc|sa|nv|ag|pvt|pte)\b",
    re.I,
)
# Trailing noise agencies append to otherwise identical titles.
_TITLE_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*[\)\]]\s*$|\s*[-–—|,]\s*(remote|hybrid|onsite|on-site|urgent|new|w2|c2c|full[- ]?time|part[- ]?time|contract)\b.*$",
    re.I,
)
# Requisition ids: either a #-number, or a req/job/posting id keyword plus a number.
_REQ_ID = re.compile(
    r"#\s*\d+\w*"
    r"|\b(?:req|requisition|job|posting)?\s*(?:id|no)\b\.?\s*[-:#]?\s*\d+\w*"
    r"|\b(?:req|requisition)\b\.?\s*[-:#]?\s*\d+\w*",
    re.I,
)
# Separators left dangling once noise is stripped off the end.
_TRAILING_SEP = re.compile(r"[\s\-–—|,:/]+$")


def normalize_company(company: str) -> str:
    """Fold a company name down to a comparable key."""
    text = (company or "").lower()
    text = _COMPANY_SUFFIXES.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_title(title: str) -> str:
    """Fold a job title down to a comparable key.

    Strips requisition ids and trailing decorations, so "Backend Engineer",
    "Backend Engineer (Remote)" and "Backend Engineer - Req #12345" all match.
    """
    text = (title or "").lower()
    text = _REQ_ID.sub(" ", text)
    previous = None
    while previous != text:  # peel repeated trailing decorations
        previous = text
        text = _TITLE_NOISE.sub("", text).strip()
        text = _TRAILING_SEP.sub("", text).strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def fingerprint(company: str, title: str) -> str:
    """Identity of a *role*, as opposed to a posting. Same role reposted under
    a new URL produces the same fingerprint."""
    return f"{normalize_company(company)}|{normalize_title(title)}"


def company_is_excluded(company: str, excluded: list) -> bool:
    """True if the company matches any blocklist entry, allowing for suffix
    and punctuation differences in either direction."""
    key = normalize_company(company)
    if not key:
        return False
    for entry in excluded:
        blocked = normalize_company(entry)
        if blocked and (blocked in key or key in blocked):
            return True
    return False


def _clean(value):
    """Normalise pandas/NumPy NaN and stray whitespace into clean strings."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "nat") else text


def _format_salary(row) -> str:
    low, high = row.get("min_amount"), row.get("max_amount")
    currency = _clean(row.get("currency")) or "USD"
    interval = _clean(row.get("interval"))
    parts = []
    for amount in (low, high):
        amount = _clean(amount)
        if amount:
            try:
                parts.append(f"{float(amount):,.0f}")
            except ValueError:
                parts.append(amount)
    if not parts:
        return ""
    salary = " - ".join(dict.fromkeys(parts))
    return f"{currency} {salary}" + (f" / {interval}" if interval else "")


def construct_skillsire_job_url(job_id) -> str:
    return f"https://www.skillsire.com/job/jobs-enlisting/all-jobs?jobId={job_id}"


def fetch_jobs_from_skillsire(search_term: str, hours: int, limit: int) -> list:
    """Page through the Skillsire API until we hit the result count or the limit."""
    payload_base = {
        "loc": "United States",
        "cc": "us",
        "type": "country",
        "dp": "p" + ("24" if hours > 1 else "1") + "h",
        "query": search_term,
    }
    headers = {"Content-Type": "application/json"}
    all_jobs, offset = [], 0

    while True:
        payload = dict(payload_base, offset=offset)
        try:
            response = requests.post(
                SKILLSIRE_API, data=json.dumps(payload), headers=headers, timeout=30
            )
        except requests.RequestException as exc:
            log.warning("Skillsire request failed: %s", exc)
            break

        if response.status_code != 200:
            log.warning("Skillsire returned status %s", response.status_code)
            break

        try:
            data = response.json()
        except ValueError:
            log.warning("Skillsire returned a non-JSON response")
            break

        jobs = data.get("jobs", []) or []
        all_jobs.extend(jobs)

        total = data.get("metaData", {}).get("resultCount", 0) or 0
        total = min(total, limit)
        log.info("Skillsire: fetched %d (offset %d), %d of %d", len(jobs), offset, len(all_jobs), total)

        if not jobs or len(all_jobs) >= total:
            break
        offset += SKILLSIRE_BATCH

    normalised = []
    for job in all_jobs[:limit]:
        location = "Unknown"
        locations = job.get("jobLocations") or []
        if locations:
            location = locations[0].get("jobState") or "Unknown"
        job_id = job.get("jobId")
        if not job_id:
            continue
        normalised.append(
            {
                "job_url": construct_skillsire_job_url(job_id),
                "title": _clean(job.get("jobTitle")),
                "company": _clean(job.get("jobCompany")),
                "location": _clean(location),
                "site": "skillsire",
                "job_type": "",
                "is_remote": 0,
                "salary": "",
                "description": "",
                "date_posted": "",
            }
        )
    return normalised


def scrape_with_jobspy(settings: dict, sites: list) -> list:
    from jobspy import scrape_jobs  # imported lazily so the API boots without it

    kwargs = {
        "site_name": sites,
        "search_term": settings["search_term"],
        "results_wanted": settings["results_fetch_count"],
        "hours_old": settings["hours"],
        "country_indeed": settings["country_to_search"],
        "linkedin_fetch_description": bool(settings.get("fetch_description")),
    }
    if settings.get("location"):
        kwargs["location"] = settings["location"]
    if settings.get("job_type"):
        kwargs["job_type"] = settings["job_type"]
    if settings.get("is_remote"):
        kwargs["is_remote"] = True

    frame = scrape_jobs(**kwargs)
    if frame is None or len(frame) == 0:
        return []

    jobs = []
    for row in frame.to_dict("records"):
        url = _clean(row.get("job_url"))
        if not url:
            continue
        jobs.append(
            {
                "job_url": url,
                "title": _clean(row.get("title")),
                "company": _clean(row.get("company")),
                "location": _clean(row.get("location")),
                "site": _clean(row.get("site")),
                "job_type": _clean(row.get("job_type")),
                "is_remote": 1 if row.get("is_remote") is True else 0,
                "salary": _format_salary(row),
                "description": _clean(row.get("description"))[:20000],
                "date_posted": _clean(row.get("date_posted")),
            }
        )
    return jobs


def filter_jobs(jobs: list, settings: dict) -> list:
    """Apply role keywords, title exclusions and the company blocklist.

    Also collapses repeats of the same role within this batch, so a company
    posting one opening five times contributes one row.
    """
    include = [r.lower().strip() for r in settings.get("roles_of_interest", []) if r.strip()]
    exclude = [x.lower().strip() for x in settings.get("exclude_keywords", []) if x.strip()]
    blocked = [c for c in settings.get("exclude_companies", []) if c and c.strip()]
    collapse = bool(settings.get("collapse_duplicates", True))

    kept, seen = [], set()
    for job in jobs:
        title = (job.get("title") or "").lower()
        if include and not any(role in title for role in include):
            continue
        if exclude and any(word in title for word in exclude):
            continue
        if blocked and company_is_excluded(job.get("company", ""), blocked):
            continue

        job["fingerprint"] = fingerprint(job.get("company", ""), job.get("title", ""))
        if collapse:
            if job["fingerprint"] in seen:
                continue
            seen.add(job["fingerprint"])
        kept.append(job)
    return kept


def store_jobs(jobs: list, collapse: bool = True) -> int:
    """Insert jobs, ignoring URLs already stored. Returns the new-row count.

    With `collapse`, a job whose role fingerprint is already in the database is
    skipped too — that catches the same opening reposted under a fresh URL.
    """
    if not jobs:
        return 0
    seen = utcnow()
    inserted = 0
    with get_conn() as conn:
        known = set()
        if collapse:
            known = {
                row["fingerprint"]
                for row in conn.execute(
                    "SELECT DISTINCT fingerprint FROM jobs WHERE fingerprint IS NOT NULL"
                )
            }

        for job in jobs:
            mark = job.get("fingerprint") or fingerprint(job.get("company", ""), job.get("title", ""))
            if collapse and mark in known:
                continue
            known.add(mark)
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO jobs
                       (job_url, title, company, location, site, job_type, is_remote,
                        salary, description, date_posted, first_seen, status, fingerprint)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',?)""",
                    (
                        job["job_url"], job["title"], job["company"], job["location"],
                        job["site"], job["job_type"], job["is_remote"], job["salary"],
                        job["description"], job["date_posted"], seen, mark,
                    ),
                )
                inserted += cur.rowcount
            except sqlite3.Error as exc:
                log.warning("Could not store %s: %s", job.get("job_url"), exc)
    return inserted


def run_scrape(settings: dict) -> dict:
    """Run one full scrape cycle. Returns counts; raises only on total failure."""
    sites = [s.lower() for s in settings.get("scrape_from", [])]
    include_skillsire = "skillsire" in sites
    jobspy_sites = [s for s in sites if s in JOBSPY_SITES]

    scraped, errors = [], []

    if jobspy_sites:
        try:
            scraped.extend(scrape_with_jobspy(settings, jobspy_sites))
        except Exception as exc:  # jobspy raises a wide range of network errors
            log.exception("jobspy scrape failed")
            errors.append(f"jobspy ({', '.join(jobspy_sites)}): {exc}")

    if include_skillsire:
        try:
            scraped.extend(
                fetch_jobs_from_skillsire(
                    settings["search_term"], settings["hours"], settings["results_fetch_count"]
                )
            )
        except Exception as exc:
            log.exception("Skillsire scrape failed")
            errors.append(f"skillsire: {exc}")

    matched = filter_jobs(scraped, settings)
    new_count = store_jobs(matched, collapse=bool(settings.get("collapse_duplicates", True)))

    return {
        "scraped": len(scraped),
        "matched": len(matched),
        "new_jobs": new_count,
        "errors": errors,
    }
