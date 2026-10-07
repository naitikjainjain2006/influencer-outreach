"""Import creators gathered from directories / UGC marketplaces (Collabstr, Aspire, Grin exports, etc.).

Required columns: name, platform, profile_url, followers
Optional: engagement_rate, email, bio, country, recent_titles (separated by '|'),
          instagram_url, tiktok_url, website
Blank optional cells stay empty -> reported as 'Not Available' / 'Not Found', never guessed.
"""
import csv
import hashlib
import json
import logging

from ..storage import now_iso

log = logging.getLogger(__name__)
REQUIRED = ["name", "platform", "profile_url", "followers"]


def _num(v, cast):
    v = (v or "").strip().replace(",", "")
    if not v:
        return None
    try:
        return cast(v)
    except ValueError:
        return None


def import_csv(path: str, store) -> int:
    saved = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV is missing required columns: {missing}")
        for n, row in enumerate(reader, start=2):
            followers = _num(row.get("followers"), int)
            if not row.get("profile_url", "").strip() or not row.get("name", "").strip():
                log.warning("row %d skipped: missing name/profile_url", n)
                continue
            titles = [t.strip() for t in (row.get("recent_titles") or "").split("|") if t.strip()]
            store.upsert_influencer({
                "id": "csv:" + hashlib.sha1(row["profile_url"].strip().lower().encode()).hexdigest()[:12],
                "name": row["name"].strip(), "platform": row["platform"].strip().title(),
                "profile_url": row["profile_url"].strip(), "followers": followers,
                "engagement_rate": _num(row.get("engagement_rate"), float),
                "country": (row.get("country") or "").strip() or None,
                "description": (row.get("bio") or "")[:3000],
                "recent_titles": json.dumps(titles),
                "email": (row.get("email") or "").strip() or None,
                "email_source": "csv_import" if (row.get("email") or "").strip() else None,
                "instagram_url": (row.get("instagram_url") or "").strip() or None,
                "tiktok_url": (row.get("tiktok_url") or "").strip() or None,
                "website": (row.get("website") or "").strip() or None,
                "data_source": "csv_import", "discovered_at": now_iso(),
            })
            saved += 1
    return saved
