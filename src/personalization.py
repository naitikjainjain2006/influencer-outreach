"""Generate a personalised email pitch (60-90 words) and Instagram DM (15-30 words) per influencer.

Primary path : LLM (Anthropic API) fed ONLY with verified facts, output validated, retried with feedback.
Fallback path: rule-based composer that varies structure per influencer (used if no API key / LLM fails).
Both paths are validated by the same function, so limits are enforced regardless of generator.
"""
import hashlib
import json
import logging
import re

from .enrichment import NOT_FOUND
from .config import env

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You write short, warm, human-sounding brand outreach to micro-influencers.
STRICT RULES:
- Use ONLY the facts in the provided JSON. Never invent metrics, quotes, past collaborations, products
  they use, or that you "follow" them. You may mention a recent video ONLY by its given title.
- Mention one specific content signal (a recent video title or a content theme) and why their audience fits.
- Propose exactly the collaboration angle and offer given. No pressure, no hype, no emojis in the email.
- email_body: 60-90 words INCLUDING greeting and sign-off. Start with "Hi <name>,". Sign off with the sender name and brand.
- instagram_dm: 15-30 words, casual, no links, at most one emoji.
- subject: max 8 words, no clickbait.
- Vary phrasing; do not sound templated.
Return ONLY a JSON object: {"subject": str, "email_body": str, "instagram_dm": str}"""


def words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def display_name(name: str) -> str:
    parts = (name or "").split()
    if 2 <= len(parts) <= 3 and all(re.fullmatch(r"[A-Z][a-z'\-]+", p) for p in parts):
        return parts[0]
    return re.sub(r"[^\w\s'&.\-]", "", name or "").strip() or (name or "there")


def pick_tier(followers: int | None, cfg: dict) -> dict:
    tiers = cfg["campaign"]["collab_tiers"]
    for t in tiers:
        if followers is not None and followers <= t["max_followers"]:
            return t
    return tiers[-1]


def _short(text: str, n: int) -> str:
    return " ".join(re.sub(r"[“”\"]", "", text).split()[:n])


def signals(inf: dict, cfg: dict) -> dict:
    themes = json.loads(inf.get("themes") or "[]")
    titles = json.loads(inf.get("recent_titles") or "[]")
    best = next((t for t in titles if any(th.split()[0] in t.lower() for th in themes)), titles[0] if titles else None)
    tier = pick_tier(inf.get("followers"), cfg)
    return {
        "name": display_name(inf["name"]), "platform": inf["platform"],
        "niche": inf.get("niche") or "lifestyle", "themes": themes,
        "recent_video_title": _short(best, 8) if best else None,
        "other_recent_titles": [_short(t, 10) for t in titles[:3] if t != best],
        "bio_snippet": (inf.get("description") or "")[:200],
        "followers": inf.get("followers"), "collab_angle": tier["angle"],
        "collab_short": tier["short"], "offer": tier["offer"],
    }


def validate(msg: dict, sig: dict, cfg: dict) -> list[str]:
    p, problems = cfg["personalization"], []
    lo, hi = p["email_words"]
    n = words(msg.get("email_body"))
    if not lo <= n <= hi:
        problems.append(f"email_body has {n} words; must be {lo}-{hi}")
    lo, hi = p["dm_words"]
    n = words(msg.get("instagram_dm"))
    if not lo <= n <= hi:
        problems.append(f"instagram_dm has {n} words; must be {lo}-{hi}")
    if re.search(r"https?://|www\.", msg.get("instagram_dm", ""), re.I):
        problems.append("instagram_dm must not contain links")
    if not (msg.get("subject") or "").strip() or words(msg["subject"]) > 8:
        problems.append("subject missing or longer than 8 words")
    body = (msg.get("email_body") or "").lower()
    cues = [t.lower() for t in sig["themes"]] + [sig["niche"].lower()] + \
           [w.lower() for w in re.findall(r"[A-Za-z]{5,}", sig["recent_video_title"] or "")]
    if not any(c in body for c in cues):
        problems.append("email_body does not reference a content signal (theme/niche/recent video)")
    return problems


# ---------------------------------------------------------------- fallback --
def _pick(options: list, key: str, salt: str = ""):
    h = int(hashlib.sha1((key + salt).encode()).hexdigest(), 16)
    return options[h % len(options)]


def fallback_generate(inf: dict, sig: dict, cfg: dict) -> dict:
    c, key = cfg["campaign"], inf["id"]
    brand, desc, sender = c["brand_name"], c["brand_description"], c["sender_name"]
    theme = sig["themes"][0] if sig["themes"] else sig["niche"].lower()
    niche, title = sig["niche"].lower(), sig["recent_video_title"]

    opener = _pick([
        f"I came across your video “{title}” and really enjoyed how you approach {theme}.",
        f"Your recent video “{title}” caught my eye, especially your take on {theme}.",
        f"“{title}” stood out to me, and your {theme} content feels genuinely useful.",
    ], key, "o") if title else _pick([
        f"Your {theme} content caught my eye and feels genuinely useful.",
        f"I really enjoy the way you approach {theme}.",
    ], key, "o")
    audience = _pick([
        f"Your {niche} audience looks like a natural fit for {brand}, {desc}.",
        f"We think your {niche} community would genuinely connect with {brand}, {desc}.",
    ], key, "a")
    pitch = _pick([
        f"We'd love to explore a collaboration with you built around {sig['collab_angle']}, offering {sig['offer']}.",
        f"Specifically, we're proposing {sig['collab_angle']}, with {sig['offer']} on our side.",
    ], key, "p")
    extra = _pick(["You'd keep full creative control, and I can send the brief and timeline whenever suits you.",
                   "Your voice stays front and centre, and I'm happy to share the brief and timeline first."], key, "e")
    cta = _pick(["Would you be open to a quick chat this week?", "Is this something you'd be interested in?",
                 "Could I send over the details?"], key, "c")
    greeting, signoff = f"Hi {sig['name']},", f"Best,\n{sender}\n{brand}"

    lo, hi = cfg["personalization"]["email_words"]
    parts = [greeting, opener, audience, pitch, cta, signoff]
    if words(" ".join(parts + [extra])) <= hi:
        parts.insert(4, extra)
    if words(" ".join(parts)) > hi:  # very long inputs: drop the company description
        parts[2] = f"Your {niche} audience looks like a natural fit for {brand}."
    body = "\n\n".join(parts)

    t = _short(title, 5) if title else None
    dm = _pick([
        f"Hi {sig['name']}, loved your video “{t}”. Your {niche} audience looks like a great fit for {brand}'s {sig['collab_short']}. Open to details?"
        if t else f"Hi {sig['name']}, loved your {theme} content. Your {niche} audience looks like a great fit for {brand}'s {sig['collab_short']}. Open to details?",
        f"Hi {sig['name']}! Your {theme} content is great, and your audience feels like a perfect match for our {sig['collab_short']} at {brand}. Interested in hearing more?",
    ], key, "d")
    subject = _pick([f"{brand} x {sig['name']}: collab idea", f"Quick collab idea for {sig['name']}",
                     f"{sig['name']}, a collaboration with {brand}?"], key, "s")
    return {"subject": _short(subject, 8) if words(subject) > 8 else subject, "email_body": body, "instagram_dm": dm}


# --------------------------------------------------------------------- LLM --
def make_llm_client():
    key = env("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=key)
    except ImportError:
        log.warning("`anthropic` package not installed; using template fallback")
        return None


def _parse_json(text: str) -> dict:
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise ValueError("no JSON object in LLM output")
    return json.loads(text[s:e + 1])


def llm_generate(client, sig: dict, cfg: dict, model: str) -> tuple[dict | None, list[str]]:
    c = cfg["campaign"]
    payload = {"brand": c["brand_name"], "brand_description": c["brand_description"],
               "sender_name": c["sender_name"], "influencer": sig}
    problems: list[str] = []
    for attempt in range(cfg["personalization"]["max_llm_retries"]):
        user = json.dumps(payload, ensure_ascii=False)
        if problems:
            user += "\n\nYour previous attempt failed validation. Fix these and return JSON only:\n- " + "\n- ".join(problems)
        try:
            resp = client.messages.create(model=model, max_tokens=700, system=SYSTEM_PROMPT,
                                          messages=[{"role": "user", "content": user}])
            msg = _parse_json("".join(b.text for b in resp.content if getattr(b, "type", "") == "text"))
        except Exception as e:
            problems = [f"output was not valid JSON or the API call failed ({type(e).__name__})"]
            log.warning("LLM attempt %d failed: %s", attempt + 1, e)
            continue
        problems = validate(msg, sig, cfg)
        if not problems:
            return msg, []
    return None, problems


def run_personalization(store, cfg: dict, use_llm: bool = True, force: bool = False, client=None) -> dict:
    client = client if client is not None else (make_llm_client() if use_llm else None)
    model = env("LLM_MODEL", "claude-sonnet-5-5")
    stats = {"llm": 0, "template_fallback": 0, "skipped_existing": 0}
    for inf in store.influencers("shortlisted"):
        if store.get_message(inf["id"]) and not force:
            stats["skipped_existing"] += 1
            continue
        sig = signals(inf, cfg)
        msg, generator = None, "template_fallback"
        if client:
            msg, problems = llm_generate(client, sig, cfg, model)
            if msg:
                generator = f"llm:{model}"
            else:
                log.warning("LLM output rejected for %s (%s); using fallback", inf["name"], problems)
        if not msg:
            msg = fallback_generate(inf, sig, cfg)
            problems = validate(msg, sig, cfg)
            if problems:
                log.error("fallback message for %s failed validation: %s", inf["name"], problems)
        store.upsert_message(inf["id"], email_subject=msg["subject"], email_body=msg["email_body"],
                             instagram_dm=msg["instagram_dm"], collab_angle=sig["collab_angle"], generator=generator)
        stats["llm" if generator.startswith("llm") else "template_fallback"] += 1
    return stats
