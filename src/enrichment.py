"""Enrich shortlisted influencers using only information the creator has made public.

Email: found ONLY if literally present in the channel description or on the creator's own linked
page (mailto: / visible address). Otherwise stored as 'Not Found'. Emails are never guessed or generated.
"""
import logging
import re
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from .storage import now_iso

log = logging.getLogger(__name__)
NOT_FOUND = "Not Found"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
IG_LINK = re.compile(r"instagram\.com/([A-Za-z0-9_.]{1,30})/?", re.I)
IG_TEXT = re.compile(r"(?:instagram|insta|\big)\s*[:\-]?\s*@([A-Za-z0-9_.]{2,30})", re.I)
TT_LINK = re.compile(r"tiktok\.com/@([A-Za-z0-9_.]{1,30})", re.I)
IG_RESERVED = {"p", "reel", "reels", "explore", "accounts", "stories", "tv", "about", "legal", "directory"}
BAD_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
BAD_EMAIL_DOMAINS = {"example.com", "sentry.io", "domain.com", "email.com"}
SOCIAL_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com",
                "twitter.com", "x.com", "twitch.tv", "pinterest.com", "snapchat.com")


def find_emails(text: str) -> list[str]:
    out = []
    for m in EMAIL_RE.findall(text or ""):
        e = m.strip(".").lower()
        if e.endswith(BAD_EMAIL_SUFFIX) or e.split("@")[1] in BAD_EMAIL_DOMAINS:
            continue
        if e not in out:
            out.append(e)
    return out


def find_instagram(text: str) -> str | None:
    for m in IG_LINK.finditer(text or ""):
        if m.group(1).lower() not in IG_RESERVED:
            return f"https://www.instagram.com/{m.group(1)}/"
    m = IG_TEXT.search(text or "")
    return f"https://www.instagram.com/{m.group(1)}/" if m else None


def find_websites(text: str) -> list[str]:
    urls = []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".,;")
        if not any(h in (urlparse(u).netloc or "").lower() for h in SOCIAL_HOSTS) and u not in urls:
            urls.append(u)
    return urls


class PageFetcher:
    """Polite fetcher: honours robots.txt, identifies itself, short timeout, size-capped."""

    def __init__(self, ua: str, timeout: int, delay: float, session=None):
        self.ua, self.timeout, self.delay = ua, timeout, delay
        self.s = session or requests.Session()
        self._robots: dict[str, RobotFileParser | None] = {}

    def _allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=self.timeout, headers={"User-Agent": self.ua})
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                self._robots[base] = rp
            except requests.RequestException:
                self._robots[base] = None
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.ua, url)

    def get(self, url: str) -> str:
        if not self._allowed(url):
            log.info("robots.txt disallows %s", url)
            return ""
        time.sleep(self.delay)
        try:
            r = self.s.get(url, timeout=self.timeout, headers={"User-Agent": self.ua})
            return r.text[:500_000] if r.status_code == 200 else ""
        except requests.RequestException as e:
            log.info("fetch failed %s: %s", url, e)
            return ""


def enrich_one(rec: dict, cfg: dict, fetcher: PageFetcher | None) -> dict:
    desc = rec.get("description") or ""
    out = {"enriched_at": now_iso()}

    email, source = rec.get("email"), rec.get("email_source")
    if not email or email == NOT_FOUND:
        found = find_emails(desc)
        email, source = (found[0], "channel_description") if found else (None, None)

    ig = rec.get("instagram_url") or find_instagram(desc)
    tt_m = TT_LINK.search(desc)
    tiktok = rec.get("tiktok_url") or (f"https://www.tiktok.com/@{tt_m.group(1)}" if tt_m else None)
    sites = find_websites(desc)
    website = rec.get("website") or (sites[0] if sites else None)

    if fetcher and website and cfg["enrichment"].get("fetch_websites", True) and (not email or not ig):
        html = fetcher.get(website)
        if not email:
            mailto = re.findall(r"mailto:([^\"'?>\s]+)", html, re.I)
            found = find_emails(" ".join(mailto)) or find_emails(re.sub(r"<[^>]+>", " ", html))
            if found:
                email, source = found[0], f"website:{website}"
        ig = ig or find_instagram(html)

    out.update({
        "email": email or NOT_FOUND, "email_source": source,
        "instagram_url": ig, "tiktok_url": tiktok, "website": website,
        # Public APIs do not expose audience demographics; never estimated or invented.
        "audience_age": "Not Available (needs creator-authorised analytics)",
        "audience_gender": "Not Available (needs creator-authorised analytics)",
        "audience_geo": rec.get("country") or "Not Available (needs creator-authorised analytics)",
    })
    return out


def run_enrichment(store, cfg: dict) -> dict:
    e = cfg["enrichment"]
    fetcher = PageFetcher(e["user_agent"], e["website_timeout_s"], e["request_delay_s"]) \
        if e.get("fetch_websites", True) else None
    n = found = 0
    for rec in store.influencers("shortlisted"):
        try:
            data = enrich_one(rec, cfg, fetcher)
            store.update_influencer(rec["id"], **data)
            n += 1
            found += data["email"] != NOT_FOUND
        except Exception as ex:
            log.warning("enrichment error for %s: %s", rec.get("id"), ex)
    return {"enriched": n, "emails_found": found, "emails_not_found": n - found}
