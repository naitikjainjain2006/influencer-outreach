"""Sending layer: email via SMTP (live) or simulation (default), manual queue for Instagram DMs.

Safeguards: only approved messages, only real (found) emails, dedupe by email, suppression list,
append-only outreach log, per-send error isolation. Instagram DMs are NEVER auto-sent: cold DMs are
not available through Meta's official API, so we create a manual-send queue instead of bypassing limits.
"""
import csv
import logging
import re
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from .config import env
from .enrichment import NOT_FOUND
from .storage import now_iso

log = logging.getLogger(__name__)
VALID_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _suppressed(cfg: dict) -> set[str]:
    p = Path(cfg["sending"].get("suppression_file", ""))
    if not p.is_file():
        return set()
    return {l.strip().lower() for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}


def build_email(inf: dict, msg: dict, cfg: dict, sender_email: str) -> EmailMessage:
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = msg["email_subject"], sender_email, inf["email"]
    m["Reply-To"] = sender_email
    m.set_content(msg["email_body"].rstrip() + "\n\n" + cfg["campaign"].get("footer", "").strip() + "\n")
    return m


def _smtp_connect():
    host, port = env("SMTP_HOST"), int(env("SMTP_PORT", "587") or 587)
    user, pw = env("SMTP_USER"), env("SMTP_PASSWORD")
    if not (host and user and pw):
        raise RuntimeError("SMTP_HOST / SMTP_USER / SMTP_PASSWORD must be set for --live sending")
    s = smtplib.SMTP(host, port, timeout=30)
    s.starttls()
    s.login(user, pw)
    return s


def _queue_dm(store, cfg: dict, inf: dict, msg: dict) -> bool:
    if store.has_log(inf["id"], "instagram_dm", "queued_manual"):
        return False
    path = Path(cfg["sending"]["dm_queue_csv"])
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["Influencer", "Instagram URL", "DM Text", "Sent (Y/N)", "Date Sent", "Notes"])
        w.writerow([inf["name"], inf.get("instagram_url") or "NOT FOUND - search manually",
                    msg["instagram_dm"], "N", "", ""])
    store.log_outreach(influencer_id=inf["id"], name=inf["name"], email=inf.get("email"),
                       channel="instagram_dm", sent=0, status="queued_manual",
                       detail="Manual send required (no official API for cold DMs)", mode="manual")
    return True


def send_outreach(store, cfg: dict, live: bool = False, limit: int | None = None, smtp=None) -> dict:
    mode = "live" if live else "dry_run"
    sender_email = env("SENDER_EMAIL") or env("SMTP_USER") or "sender@example.com"
    outbox = Path(cfg["sending"]["outbox_dir"])
    suppressed = _suppressed(cfg)
    delay = cfg["sending"].get("delay_between_sends_s", 0)
    stats = {"sent": 0, "simulated": 0, "failed": 0, "skipped_no_email": 0, "skipped_duplicate": 0,
             "skipped_not_approved": 0, "skipped_suppressed": 0, "dm_queued": 0}
    own_conn = False
    attempted = 0

    try:
        for inf in store.influencers("shortlisted"):
            msg = store.get_message(inf["id"])
            if not msg:
                continue
            if not msg["approved"]:
                stats["skipped_not_approved"] += 1
                continue

            if _queue_dm(store, cfg, inf, msg):
                stats["dm_queued"] += 1

            base = dict(influencer_id=inf["id"], name=inf["name"], email=inf.get("email"),
                        channel="email", mode=mode)
            email = (inf.get("email") or "").strip()
            if not email or email == NOT_FOUND or not VALID_EMAIL.match(email):
                stats["skipped_no_email"] += 1
                if not store.has_log(inf["id"], "email", "skipped_no_email"):
                    store.log_outreach(**base, sent=0, status="skipped_no_email", detail="no valid public email")
                continue
            if email.lower() in suppressed:
                stats["skipped_suppressed"] += 1
                continue
            if store.already_contacted(email, mode):
                stats["skipped_duplicate"] += 1
                if not store.has_log(inf["id"], "email", "skipped_duplicate"):
                    store.log_outreach(**base, sent=0, status="skipped_duplicate", detail="already contacted")
                continue
            if limit is not None and attempted >= limit:
                break
            attempted += 1

            email_msg = build_email(inf, msg, cfg, sender_email)
            try:
                if live:
                    if smtp is None:
                        smtp, own_conn = _smtp_connect(), True
                    smtp.send_message(email_msg)
                    if delay:
                        time.sleep(delay)
                else:
                    outbox.mkdir(parents=True, exist_ok=True)
                    safe = re.sub(r"[^A-Za-z0-9]+", "_", inf["id"])
                    (outbox / f"{safe}.eml").write_bytes(bytes(email_msg))
                store.mark_contacted(email, mode, inf["id"])
                store.log_outreach(**base, sent=1, sent_at=now_iso(),
                                   status="sent" if live else "simulated_sent",
                                   detail="" if live else "dry-run: .eml written to outbox")
                stats["sent" if live else "simulated"] += 1
            except (smtplib.SMTPException, OSError, RuntimeError) as e:
                log.error("send to %s failed: %s", email, e)
                store.log_outreach(**base, sent=0, status="failed", detail=str(e)[:300])
                stats["failed"] += 1
                if isinstance(e, RuntimeError):
                    break  # misconfiguration: no point continuing
    finally:
        if own_conn and smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass
    return stats
