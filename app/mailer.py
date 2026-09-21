"""Optional email notifications for newly found jobs."""

import csv
import io
import logging
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("jsu.mailer")

FIELDS = ["title", "company", "location", "site", "salary", "job_url"]


def jobs_to_csv(jobs: list) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for job in jobs:
        writer.writerow({field: job.get(field, "") for field in FIELDS})
    return buffer.getvalue()


def _send(settings: dict, subject: str, body: str, attachment: tuple | None = None) -> None:
    message = MIMEMultipart()
    message["From"] = settings["from_email"]
    message["To"] = settings["to_email"]
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))

    if attachment:
        filename, content = attachment
        part = MIMEBase("application", "octet-stream")
        part.set_payload(content.encode("utf-8"))
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={filename}")
        message.attach(part)

    port = int(settings.get("email_port") or 587)
    if port == 465:
        server = smtplib.SMTP_SSL(settings["email_smtp"], port, timeout=30)
    else:
        server = smtplib.SMTP(settings["email_smtp"], port, timeout=30)
        server.starttls()
    try:
        server.login(settings["from_email"], settings["email_password"])
        server.sendmail(settings["from_email"], settings["to_email"], message.as_string())
    finally:
        server.quit()


def send_jobs_email(settings: dict, jobs: list) -> None:
    """Email a CSV of the jobs found in this run."""
    if not jobs:
        return
    lines = [f"{j.get('title','')} — {j.get('company','')} ({j.get('location','')})" for j in jobs[:25]]
    if len(jobs) > 25:
        lines.append(f"...and {len(jobs) - 25} more (see attached CSV).")
    body = f"JobScraperUltimate found {len(jobs)} new job(s):\n\n" + "\n".join(lines)
    _send(settings, f"{len(jobs)} new job listing(s)", body, ("new_jobs.csv", jobs_to_csv(jobs)))
    log.info("Sent email for %d jobs", len(jobs))


def send_test_email(settings: dict) -> None:
    _send(settings, "JobScraperUltimate test email",
          "This is a test message. Your SMTP settings are working.")
