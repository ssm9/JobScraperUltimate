# JobScraperUltimate

A self-hosted job scraper with a web UI, packaged as a **TrueNAS SCALE custom app**.

It scrapes LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs and Skillsire on a
schedule, filters postings by role keywords, de-duplicates them, and presents everything
in a browser UI where you can search, triage and track what you've applied to.

> Forked from [Samirasimha/JobScraperUltimate](https://github.com/Samirasimha/JobScraperUltimate).
> The original is a single script with settings hard-coded at the top of the file that
> appends results to CSVs. This fork keeps the same scraping and filtering logic but adds
> a web UI, a SQLite database, settings editable at runtime, and container packaging.

## Features

- **Jobs board** — search, filter by source/status, sort, paginate
- **Triage workflow** — mark jobs as saved, applied or hidden, individually or in bulk
- **Spam control** — block a company in one click, and collapse repeat postings of the same role
- **Settings screen** — every setting from the original script, editable in the browser; no file edits, no restarts
- **Persistent storage** — SQLite on a mounted dataset; de-duplication across all runs, not just today's CSV
- **Scheduler** — automatic scrapes on a configurable interval, plus a "Scrape now" button
- **Activity log** — every run recorded with counts and any errors
- **Email notifications** — optional, with a "send test email" button
- **CSV export** — export any filtered view

## Screens

| Screen | What it does |
|---|---|
| **Jobs** | The scraped listings, newest first. Stat tiles across the top, search and filters, and per-job Save / Applied / Hide actions. |
| **Settings** | Search terms, location, sources, result counts, look-back window, include/exclude keywords, schedule interval and SMTP settings. |
| **Activity** | Recent scrape runs with scraped/matched/new counts and error messages, plus database cleanup tools. |

---

## Deploying on TrueNAS SCALE

### 1. Create a dataset for app data

In the TrueNAS UI: **Datasets → Add Dataset**, e.g. `tank/apps/jobscraper`.
This holds `jobscraper.db` — your jobs and settings — and is what you back up.

Make sure it's owned by the `apps` user (uid/gid **568**), which is what the container
runs as:

```bash
chown -R 568:568 /mnt/tank/apps/jobscraper
```

### 2. Install the custom app

**Apps → Discover Apps → Custom App → Install via YAML**, then paste
[`docker-compose.yaml`](docker-compose.yaml), editing two things first:

- the host path in `volumes` to match the dataset you created
- the host port (`8188`) if something else already uses it

```yaml
services:
  jobscraper:
    image: ghcr.io/ssm9/jobscraperultimate:latest
    container_name: jobscraper
    restart: unless-stopped
    ports:
      - "8188:8000"
    volumes:
      - /mnt/tank/apps/jobscraper:/data
    environment:
      TZ: America/New_York
    user: "568:568"
```

### 3. Open the UI

`http://<truenas-ip>:8188`

Go to **Settings** first: set your search term, pick your sources, set your role keywords,
then turn on **Run scrapes automatically**. Or hit **Scrape now** to fetch immediately.

### Updating

TrueNAS pulls `:latest` on app update. To pin a version, replace `latest` with a
commit tag from [the package page](https://github.com/ssm9/JobScraperUltimate/pkgs/container/jobscraperultimate).

---

## Configuration

Everything is configured in the **Settings** screen and stored in the database — the
container needs no environment configuration beyond the following:

| Variable | Default | Purpose |
|---|---|---|
| `JSU_DATA_DIR` | `/data` | Where the SQLite database lives |
| `JSU_DB_PATH` | `$JSU_DATA_DIR/jobscraper.db` | Full override of the database path |
| `JSU_LOG_LEVEL` | `INFO` | `DEBUG` for verbose scraping logs |
| `TZ` | unset | Container timezone |

### Settings reference

| Setting | Notes |
|---|---|
| Search term | Passed to every job board, e.g. `"python developer"` |
| Location / Country | Location is optional; country applies to Indeed and Glassdoor |
| Sources | `linkedin`, `indeed`, `zip_recruiter`, `glassdoor`, `google`, `bayt`, `naukri`, `skillsire` |
| Results per source | Keep at 300 or below — higher values raise the risk of IP blocking |
| Look back (hours) | Only return postings newer than this |
| Role keywords | A job is kept if its title contains **any** of these. Empty keeps everything |
| Exclude keywords | A job is dropped if its title contains **any** of these |
| Blocked companies | Jobs from these companies are never stored. Matching ignores Inc/LLC/Ltd and punctuation, so `acme` blocks `Acme, Inc.` |
| Collapse repeat postings | Keeps one entry per company + role, even when reposted under a new URL |
| Interval | Seconds between automatic scrapes. 1800 (30 min) is a sane default |
| Email | Gmail needs an [app password](https://support.google.com/accounts/answer/185833), not your account password. Port 587 = STARTTLS, 465 = SSL |

---

## API

The UI is a thin client over a REST API, so you can script against it:

| Endpoint | Description |
|---|---|
| `GET /api/jobs` | List jobs — `q`, `status`, `site`, `company`, `sort`, `order`, `page`, `per_page` |
| `GET /api/jobs/{id}` | A single job, including its description |
| `PATCH /api/jobs/{id}` | Update `status` (`new`/`saved`/`applied`/`hidden`) or `notes` |
| `POST /api/jobs/bulk` | `{"ids": [...], "status": "..."}` |
| `DELETE /api/jobs` | Delete by `status` or `older_than_days` |
| `POST /api/jobs/collapse-duplicates` | Hide repeat postings already in the database |
| `GET /api/companies` | Companies by posting count, with distinct-role counts |
| `POST /api/companies/block` | `{"company": "...", "hide_existing": true}` |
| `POST /api/companies/unblock` | Remove a company from the blocklist |
| `GET /api/export.csv` | CSV export, honours `status` and `q` |
| `GET` / `PUT /api/settings` | Read / update settings |
| `POST /api/settings/test-email` | Verify SMTP configuration |
| `POST /api/scrape/run` | Trigger a scrape immediately |
| `GET /api/runs` | Scrape-run history |
| `GET /api/stats` | Counts, schedule state, last run |
| `GET /health` | Health check (used by the container healthcheck) |

Interactive API docs are at `/docs`.

---

## Running locally

```bash
pip install -r requirements.txt
JSU_DATA_DIR=./data uvicorn app.main:app --reload --port 8188
```

Or with Docker:

```bash
docker compose -f docker-compose.build.yaml up --build
```

The original CLI script is still present as
[`JobScraperUltimate.py`](JobScraperUltimate.py) if you prefer the CSV workflow.

---

## Too many postings from one company

Job boards are full of agencies posting the same opening a dozen times. Two mechanisms
handle this, both on by default:

**Collapsing repeat postings.** Jobs are identified by a fingerprint of company + role
rather than by URL alone, so `Backend Engineer`, `Backend Engineer (Remote)` and
`Backend Engineer - Req #12345` from the same company count as one opening. Legal suffixes
and punctuation are ignored when comparing companies, so `Acme` and `Acme, Inc.` are the
same employer. Different companies posting the same title stay separate.

Turn it off in **Settings → Filtering** if you'd rather see every posting.

**Blocking a company.** Hit **Block** on any job card to hide everything that company has
posted and skip it on future scrapes. **Settings → Filtering** lists your most frequent
companies with their posting-vs-distinct-role counts — a big gap between those two numbers
is the signature of a spammer — and lets you block or edit the list directly.

For jobs already scraped before you enabled any of this, **Activity → Collapse existing
duplicates** applies the fingerprint rule retroactively. It keeps the earliest posting of
each role and never touches anything you've saved or applied to.

## Troubleshooting

**No jobs appear after a scrape.** Check **Activity** — if `scraped` is high but `matched`
is 0, your role keywords are too narrow. Clear them to keep everything.

**Activity shows errors mentioning LinkedIn or Indeed.** The boards rate-limit aggressively.
Increase the interval, lower results per source, and wait a while. Scrapes are
best-effort: a failure on one source doesn't stop the others.

**`sqlite3.OperationalError: unable to open database file` / "Data directory is not
writable" on startup.** The mounted dataset isn't writable by uid 568. The usual cause is
that the host path in `volumes` didn't exist when the app was installed — Docker then
created it automatically as `root:root`, which the container can't write to. Check it:

```bash
ls -ld /mnt/tank/apps/jobscraper
```

If it shows `root root`, fix it and restart the app:

```bash
chown -R 568:568 /mnt/tank/apps/jobscraper
chmod 770 /mnt/tank/apps/jobscraper
```

If you'd rather keep the existing ownership, set `user:` in the compose YAML to match the
uid:gid the directory already has. From v2.0.1 the app reports the exact uid, owner and
mode on startup rather than raising a bare sqlite error.

**The app is unreachable.** Confirm the host port isn't taken by another app, and check
the container logs in **Apps → jobscraper → Logs**.

## License

MIT, as in the original. See [LICENSE](LICENSE).
