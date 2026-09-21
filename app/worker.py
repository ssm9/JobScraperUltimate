"""Background scheduler that runs scrapes on an interval."""

import logging
import threading
import time

from .db import get_conn, get_settings, utcnow
from .mailer import send_jobs_email
from .scraper import run_scrape

log = logging.getLogger("jsu.worker")


class ScrapeWorker:
    """Owns the scrape loop. Only one scrape runs at a time."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._manual_requested = False
        self._run_lock = threading.Lock()
        self.running = False
        self.next_run_at: float | None = None
        self.last_result: dict | None = None

    # --- lifecycle ---

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scrape-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> bool:
        """Ask for a scrape as soon as possible. False if one is already running."""
        if self.running:
            return False
        self._manual_requested = True
        self._wake.set()
        return True

    def reconfigure(self) -> None:
        """Wake the loop so schedule changes take effect immediately."""
        self._wake.set()

    # --- internals ---

    def _loop(self) -> None:
        next_scheduled: float | None = None

        while not self._stop.is_set():
            settings = get_settings()
            scheduled = bool(settings.get("schedule_enabled"))
            interval = max(60, int(settings.get("sleep_time") or 1800))

            now = time.time()
            if scheduled:
                if next_scheduled is None:
                    next_scheduled = now + interval
                wait_for = max(0.0, next_scheduled - now)
                self.next_run_at = next_scheduled
            else:
                next_scheduled = None
                self.next_run_at = None
                wait_for = 3600.0  # idle; a settings change wakes us sooner

            woke = self._wake.wait(timeout=wait_for)
            self._wake.clear()
            if self._stop.is_set():
                break

            if woke:
                # Either a manual trigger or a settings change. On a settings
                # change we just loop round and re-read the new configuration.
                if self._manual_requested:
                    self._manual_requested = False
                    self.execute("manual")
                    next_scheduled = time.time() + interval if scheduled else None
                continue

            if scheduled:
                self.execute("scheduled")
                next_scheduled = time.time() + max(
                    60, int(get_settings().get("sleep_time") or 1800)
                )

    def execute(self, trigger: str = "manual") -> dict:
        """Run one scrape, recording it in the runs table."""
        if not self._run_lock.acquire(blocking=False):
            return {"error": "A scrape is already running"}

        self.running = True
        settings = get_settings()
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, trigger, status) VALUES (?,?,'running')",
                (utcnow(), trigger),
            )
            run_id = cur.lastrowid

        result, status, message = {}, "success", ""
        try:
            log.info("Starting %s scrape: %r", trigger, settings["search_term"])
            result = run_scrape(settings)
            if result["errors"]:
                status = "partial" if result["new_jobs"] or result["scraped"] else "error"
                message = "; ".join(result["errors"])[:1000]

            if result["new_jobs"] and settings.get("email_send"):
                try:
                    send_jobs_email(settings, self._recent_new_jobs(result["new_jobs"]))
                except Exception as exc:
                    log.exception("Email failed")
                    message = (message + "; " if message else "") + f"email failed: {exc}"
                    status = "partial" if status == "success" else status

            log.info("Scrape done: %s", result)
        except Exception as exc:
            log.exception("Scrape failed")
            status, message = "error", str(exc)[:1000]
            result = {"scraped": 0, "matched": 0, "new_jobs": 0, "errors": [str(exc)]}
        finally:
            with get_conn() as conn:
                conn.execute(
                    """UPDATE runs SET finished_at=?, status=?, scraped=?, matched=?,
                       new_jobs=?, message=? WHERE id=?""",
                    (utcnow(), status, result.get("scraped", 0), result.get("matched", 0),
                     result.get("new_jobs", 0), message, run_id),
                )
            self.running = False
            self.last_result = {"run_id": run_id, "status": status, **result}
            self._run_lock.release()

        return self.last_result

    @staticmethod
    def _recent_new_jobs(limit: int) -> list:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT title, company, location, site, salary, job_url "
                "FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


worker = ScrapeWorker()
