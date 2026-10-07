# Automated Micro-Influencer Outreach System

End-to-end pipeline: **Discovery → Filtering → Enrichment → AI Personalization → Review → Sending → Tracking**.
Implemented niche/category: **Fashion & Beauty**. Platform: **YouTube** (official Data API), plus a CSV importer for
directories / UGC marketplaces (Collabstr, Aspire, Grin exports).

> **Data integrity:** the system never fabricates influencers, emails, or metrics. Anything not publicly available is
> stored as `Not Found` (email) or `Not Available` (everything else). The synthetic records in `tests/` are clearly
> labelled `TEST_` fixtures that exercise code paths offline and are never written to the real dataset.

```
YouTube API / CSV ─► discover ─► SQLite ─► filter ─► enrich ─► personalize ─► review ─► send ─► tracker
                                              │          │            │           │         │
                                        pass/fail +   email, IG,   email + DM,   human     dry-run or SMTP,
                                        reasons       website      validated    approval   dedupe, DM queue
```

## 1. Technology stack
Python 3.10+, `requests`, SQLite (stdlib), PyYAML, python-dotenv, Anthropic SDK (optional), `smtplib` (stdlib), `unittest`.

## 2. APIs / tools used
| Tool | Purpose |
|---|---|
| YouTube Data API v3 (`search`, `channels`, `playlistItems`, `videos`) | discovery, follower counts, recent videos, likes/comments/views |
| Anthropic Messages API | personalised email + DM generation (model set via `LLM_MODEL`) |
| SMTP (Gmail App Password works) | live email delivery |
| Creator's own public links / site (robots.txt respected) | publicly listed contact email, Instagram handle |

## 3. Data sources
- **YouTube Data API** (primary, official, ToS-compliant, no scraping).
- **CSV import** (`discover --source csv --csv file.csv`) for Collabstr/Aspire/Grin/newsletter lists you have legitimate access to. Required columns: `name, platform, profile_url, followers`; optional: `engagement_rate, email, bio, country, recent_titles (|-separated), instagram_url, tiktok_url, website`.

## 4. Discovery methodology
Each query in `config.yaml → discovery.youtube.queries` (e.g. "skincare routine", "thrift haul", "capsule wardrobe") is searched per region code with channel-type search (2 pages x 50 results). Channel IDs are de-duplicated, then for each channel we fetch statistics and its 10 most recent uploads (titles, views, likes, comments, dates).
- **Followers** = public subscriber count (hidden counts are recorded as unavailable and fail filtering).
- **Engagement rate** = mean over recent videos of `(likes + comments) / views x 100`; `None` if fewer than 3 videos expose likes + views (never estimated).
- Default config uses roughly 6-7k of the 10k daily free quota units. Check the count with `python main.py stats`; if it is under 50, add queries/regions or import a CSV.

## 5. Filtering logic (`src/classification.py`, `src/filtering.py`)
Every discovered influencer is classified, then evaluated against 7 checks. **All must pass**; the verdict and every reason are stored (`filter_summary`, `filter_checks`) and exported.

| Check | Rule (configurable) |
|---|---|
| platform | in `allowed_platforms` |
| followers | 5,000-100,000 (micro-influencer definition) |
| engagement | >= 2.0% |
| niche relevance | share of recent video titles matching the Fashion/Beauty lexicon >= 0.30, niche label in allowed list |
| recent activity | last post within 90 days |
| geography | optional allow-list; unknown country is not penalised |
| brand safety | no blocklist terms (casino, betting, nsfw, ...) in bio/keywords/titles |

Niche label: Beauty, Fashion, or Fashion & Beauty (each side needs >= 20% of recent titles). Themes (skincare, makeup, haircare, hauls, streetwear, ...) come from the same lexicon. This is deterministic and explainable by design.

## 6. Enrichment process (`src/enrichment.py`)
For shortlisted creators only: **email** (regex over the channel description; else `mailto:`/visible address on the creator's own linked website, fetched politely with robots.txt check, UA, timeout, size cap), **Instagram handle** (instagram.com links or explicit "IG: @handle" text), TikTok link, website, country. **Audience age/gender are marked `Not Available`** because public APIs don't expose them. Emails are never guessed, pattern-generated, or bought; no email -> `Not Found`, and the `email_source` is recorded for auditability.

## 7. AI model and prompt (`src/personalization.py`)
Model: configurable via `LLM_MODEL` (default `claude-sonnet-5-5`). The system prompt forces *grounded* output: use only supplied facts, mention at most the given recent video title, propose only the given angle/offer, no invented claims, JSON-only response. The user message is a JSON object of verified signals (name, platform, niche, themes, recent video title, bio snippet, follower tier -> collab angle and offer).

## 8. Personalization logic
1. **Collab angle by follower tier** (config): UGC (<20k), affiliate (<50k), paid sponsorship (larger).
2. **LLM generates** subject + email (60-90 words incl. greeting/sign-off) + DM (15-30 words).
3. **Validator** checks word counts, no links in DM, subject length, and that the email references a real content signal. Failures are fed back to the model for up to 3 retries.
4. **Fallback composer** (no API key / repeated failure): picks among phrasing variants per influencer (hash-seeded) so messages still differ. It passes the same validator (tested across hundreds of input combinations).
5. Each message records its `generator` (`llm:<model>` or `template_fallback`) for transparency.

## 9. Sending mechanism (`src/sender.py`)
- Only **approved** messages (human review gate: `review --approve-all` or per-id) and only creators with a **valid found email**.
- **Dry-run by default**: writes `.eml` files to `data/outbox/` and logs `simulated_sent`. `--live` sends over SMTP.
- **Duplicate prevention**: `contacted` table keyed on email (a live send always blocks; dry-runs only block dry-runs); plus `data/suppression.txt` opt-out list.
- **Outreach log** (`outreach_log` table -> `data/outreach_tracker.csv`): influencer, channel, email, message generated, sent, date, status, detail. Failures are logged per recipient and never abort the batch.
- Every email gets the configured footer (opt-out + postal address) for CAN-SPAM/GDPR-style hygiene.
- **Instagram DMs are not auto-sent.** Meta's official messaging API does not support cold DMs to arbitrary creators, and we don't bypass platform limits. DMs go to `data/dm_manual_queue.csv` (name, IG URL, text, sent Y/N, date) for manual sending.

## 10. Limitations
- YouTube subscriber counts are rounded by the API; likes may be hidden; engagement is a recent-10-video sample.
- Audience demographics are not publicly available; fields are marked as such.
- Emails exist for only a subset of creators; the rest stay `Not Found` (by design).
- Instagram/TikTok discovery is not scraped (ToS); use the CSV importer with data you're permitted to use.
- Niche classification is keyword-based (English lexicon); non-English channels may be under-classified.
- Subscriber-based tiers and keyword brand-safety are coarse heuristics, not a substitute for human vetting; that's what the review gate is for.
- Not tested against live YouTube/SMTP/LLM endpoints in the build environment (no internet); the logic is covered by offline tests with fakes. Run a small live batch first (`send --live --limit 3`).

## 11. Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add YOUTUBE_API_KEY (+ ANTHROPIC_API_KEY, SMTP creds for live send)
# edit config.yaml -> campaign (brand, sender, footer/address), filters, queries

python main.py discover         # or: discover --source csv --csv creators.csv
python main.py filter
python main.py enrich
python main.py stats            # confirm >= 50 discovered
python main.py personalize      # add --no-llm to force fallback
python main.py export           # inspect data/personalized_messages.csv
python main.py review --approve-all
python main.py send             # dry-run; later: send --live --limit 3
python main.py export           # final dataset + tracker
python -m unittest discover -s tests -v
```
Outputs in `data/`: `influencer_dataset.csv`, `personalized_messages.csv`, `outreach_tracker.csv`, `dm_manual_queue.csv`, `outbox/*.eml`, `outreach.db`.

YouTube key: Google Cloud Console -> new project -> enable "YouTube Data API v3" -> Credentials -> API key.

## 12. Scalability (50 -> 500+)
Stages are independent and idempotent (upserts keyed by platform ID; re-runs skip existing messages), state lives in SQLite (swap for Postgres via `Store`), discovery is bounded by config caps and handles quota exhaustion gracefully (keeps what it fetched). To scale: more queries/regions, multiple API keys or quota increase, a queue/worker for enrichment, and cron/n8n/Make triggers calling the CLI stages. New platforms plug in as a module under `src/discovery/` that writes the same record shape.

## Layout
```
main.py            CLI            config.yaml   all tunables (brand, filters, lexicon, tiers)
src/discovery/     youtube.py, csv_import.py      src/classification.py, filtering.py
src/enrichment.py  src/personalization.py         src/sender.py, exporter.py, storage.py
tests/             offline end-to-end tests (TEST_ fixtures only)
```
## Test Run Results
- Brand used: **Glowlane**, a fictional demo brand created for this assignment.
- Discovered: 549 | Shortlisted: 38 | Rejected: 511
- Public emails found: 15 (remaining marked "Not Found")
- Messages generated: 38 (email + Instagram DM each)
- Sending was run in **dry-run mode**: no real emails were sent. Instagram DMs are in a manual queue because Meta's official API does not support cold DMs.
- Discovery note: an initial channel-name search returned many low-quality channels, which the filters correctly rejected with reasons. Discovery was then switched to video-based search to find active creators.
