"""Rule-based filtering. Every influencer gets a pass/fail verdict plus per-check reasons."""
import json
import logging
import re
from datetime import datetime, timezone

from .classification import classify

log = logging.getLogger(__name__)


def _days_since(iso: str | None):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - d).days
    except ValueError:
        return None


def evaluate(rec: dict, cls: dict, cfg: dict) -> list[dict]:
    f = cfg["filtering"]
    checks = []

    def add(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    add("platform", rec.get("platform") in f["allowed_platforms"], f"platform={rec.get('platform')}")

    fol = rec.get("followers")
    if fol is None:
        add("followers", False, "follower count unavailable (hidden or missing)")
    else:
        add("followers", f["min_followers"] <= fol <= f["max_followers"],
            f"{fol:,} followers (allowed {f['min_followers']:,}-{f['max_followers']:,})")

    eng = rec.get("engagement_rate")
    if eng is None:
        add("engagement", False, "engagement rate unavailable (too few videos with public likes/views)")
    else:
        add("engagement", eng >= f["min_engagement_pct"], f"{eng}% (min {f['min_engagement_pct']}%)")

    add("niche_relevance",
        cls["relevance_score"] >= f["min_relevance"] and cls["niche"] in f["allowed_niches"],
        f"niche={cls['niche']}, relevance={cls['relevance_score']} (min {f['min_relevance']})")

    age = _days_since(rec.get("last_post_date"))
    if age is None:
        add("recent_activity", True, "last post date unknown (not penalised)")
    else:
        add("recent_activity", age <= f["max_days_since_last_post"],
            f"last post {age} days ago (max {f['max_days_since_last_post']})")

    allowed = f.get("allowed_countries") or []
    country = rec.get("country")
    if allowed and country:
        add("geography", country in allowed, f"country={country} (allowed {allowed})")
    else:
        add("geography", True, f"country={country or 'unknown'} (no restriction applied)")

    haystack = f"{rec.get('description') or ''} {rec.get('keywords') or ''} {rec.get('recent_titles') or ''}"
    hits = [w for w in f.get("brand_safety_blocklist", []) if re.search(r"(?<!\w)%s(?!\w)" % re.escape(w), haystack, re.I)]
    add("brand_safety", not hits, f"blocked terms found: {hits}" if hits else "no blocked terms")
    return checks


def run_filter(store, cfg: dict) -> dict:
    counts = {"shortlisted": 0, "rejected": 0}
    for rec in store.influencers():
        try:
            cls = classify(rec, cfg["lexicon"])
            checks = evaluate(rec, cls, cfg)
            failed = [f'{c["check"]}: {c["detail"]}' for c in checks if not c["passed"]]
            status = "rejected" if failed else "shortlisted"
            summary = ("FAIL: " + "; ".join(failed)) if failed else "PASS: all checks met"
            store.update_influencer(rec["id"], **cls, status=status,
                                   filter_summary=summary, filter_checks=json.dumps(checks))
            counts[status] += 1
        except Exception as e:
            log.warning("filter error for %s: %s", rec.get("id"), e)
    return counts
