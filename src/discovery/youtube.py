"""YouTube Data API v3 discovery. Uses only the official API (no scraping).

Quota (default 10,000 units/day): search.list = 100, channels/playlistItems/videos.list = 1 each.
"""
import json
import logging
import time

import requests

from ..storage import now_iso

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/youtube/v3"


class QuotaExceeded(Exception):
    pass


class YouTubeClient:
    def __init__(self, api_key: str, session=None, max_retries: int = 3, backoff: float = 1.5):
        if not api_key:
            raise ValueError("YOUTUBE_API_KEY is not set")
        self.key, self.s = api_key, session or requests.Session()
        self.max_retries, self.backoff = max_retries, backoff

    def _get(self, endpoint: str, **params) -> dict | None:
        params["key"] = self.key
        for attempt in range(self.max_retries):
            try:
                r = self.s.get(f"{API}/{endpoint}", params=params, timeout=20)
            except requests.RequestException as e:
                log.warning("network error on %s (attempt %d): %s", endpoint, attempt + 1, e)
                time.sleep(self.backoff ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403 and "quota" in r.text.lower():
                raise QuotaExceeded(r.text[:200])
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(self.backoff ** attempt)
                continue
            if r.status_code == 404:
                log.info("YouTube %s -> 404 (not found), skipping", endpoint)
                return None
            log.error("YouTube %s -> HTTP %s: %s", endpoint, r.status_code, r.text[:200])
            return None
        return None

    def search_channel_ids(self, query: str, region: str | None, pages: int, kind: str = "channel") -> list[str]:
        """kind='channel' matches channel names; kind='video' finds creators who actually post on the topic."""
        ids, token = [], None
        for _ in range(pages):
            params = dict(part="snippet", type=kind, q=query, maxResults=50)
            if region:
                params["regionCode"] = region
            if token:
                params["pageToken"] = token
            data = self._get("search", **params)
            if not data:
                break
            for item in data.get("items", []):
                cid = item.get("id", {}).get("channelId") or item.get("snippet", {}).get("channelId")
                if cid:
                    ids.append(cid)
            token = data.get("nextPageToken")
            if not token:
                break
        return ids

    def channels(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("channels", part="snippet,statistics,contentDetails,brandingSettings",
                             id=",".join(ids[i:i + 50]), maxResults=50)
            out += (data or {}).get("items", [])
        return out

    def recent_videos(self, uploads_playlist: str, n: int) -> list[dict]:
        pl = self._get("playlistItems", part="contentDetails", playlistId=uploads_playlist, maxResults=n)
        vids = [i["contentDetails"]["videoId"] for i in (pl or {}).get("items", [])
                if i.get("contentDetails", {}).get("videoId")]
        if not vids:
            return []
        data = self._get("videos", part="snippet,statistics", id=",".join(vids))
        return (data or {}).get("items", [])


def engagement_rate(videos: list[dict], min_videos: int = 3) -> float | None:
    """Mean per-video (likes + comments) / views, as a percentage.

    Returns None (never a guess) if fewer than `min_videos` videos expose likes + views.
    """
    rates = []
    for v in videos:
        s = v.get("statistics", {})
        try:
            views = int(s.get("viewCount", 0))
            if views <= 0 or "likeCount" not in s:
                continue
            rates.append((int(s["likeCount"]) + int(s.get("commentCount", 0))) / views * 100)
        except (TypeError, ValueError):
            continue
    return round(sum(rates) / len(rates), 2) if len(rates) >= min_videos else None


def channel_to_record(ch: dict, videos: list[dict]) -> dict:
    sn, st = ch.get("snippet", {}), ch.get("statistics", {})
    followers = None
    if not st.get("hiddenSubscriberCount") and "subscriberCount" in st:
        followers = int(st["subscriberCount"])
    custom = sn.get("customUrl", "")
    url = f"https://www.youtube.com/{custom}" if custom.startswith("@") else \
        f"https://www.youtube.com/channel/{ch['id']}"
    views = [int(v["statistics"]["viewCount"]) for v in videos if "viewCount" in v.get("statistics", {})]
    dates = sorted(v["snippet"]["publishedAt"] for v in videos if v.get("snippet", {}).get("publishedAt"))
    return {
        "id": f"youtube:{ch['id']}",
        "name": sn.get("title", "").strip() or "Not Available",
        "platform": "YouTube",
        "profile_url": url,
        "handle": custom or None,
        "followers": followers,
        "engagement_rate": engagement_rate(videos),
        "avg_views": round(sum(views) / len(views), 1) if views else None,
        "country": sn.get("country"),
        "description": (sn.get("description") or "")[:3000],
        "keywords": ch.get("brandingSettings", {}).get("channel", {}).get("keywords", ""),
        "recent_titles": json.dumps([v["snippet"]["title"] for v in videos if v.get("snippet")]),
        "last_post_date": dates[-1] if dates else None,
        "data_source": "youtube_data_api_v3",
        "discovered_at": now_iso(),
    }


def discover(cfg: dict, store, api_key: str, client: YouTubeClient | None = None) -> int:
    yc = cfg["discovery"]["youtube"]
    client = client or YouTubeClient(api_key)
    cand: list[str] = []
    try:
        for q in yc["queries"]:
            for region in yc.get("region_codes") or [None]:
                for cid in client.search_channel_ids(q, region, yc.get("pages_per_query", 1), yc.get("search_type", "channel")):
                    if cid not in cand:
                        cand.append(cid)
                if len(cand) >= yc["max_candidates"]:
                    break
            if len(cand) >= yc["max_candidates"]:
                break
    except QuotaExceeded as e:
        log.error("YouTube quota exceeded during search; continuing with %d candidates (%s)", len(cand), e)
    cand = cand[: yc["max_candidates"]]
    log.info("found %d candidate channels", len(cand))

    saved = 0
    try:
        for ch in client.channels(cand):
            try:
                uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
                vids = client.recent_videos(uploads, yc.get("videos_per_channel", 10))
                store.upsert_influencer(channel_to_record(ch, vids))
                saved += 1
            except QuotaExceeded:
                raise
            except Exception as e:  # one bad channel must not stop the run
                log.warning("skipping channel %s: %s", ch.get("id"), e)
    except QuotaExceeded as e:
        log.error("YouTube quota exceeded; saved %d channels so far (%s)", saved, e)
    return saved