"""End-to-end tests. ALL creator data below is a synthetic TEST FIXTURE (prefix TEST_), used only to
exercise the code paths offline. It is never written to the real dataset."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import load_config
from src.discovery.csv_import import import_csv
from src.discovery.youtube import YouTubeClient, discover, engagement_rate
from src.enrichment import NOT_FOUND, find_emails, find_instagram, run_enrichment
from src.exporter import export_all
from src.filtering import run_filter
from src.personalization import fallback_generate, run_personalization, signals, validate, words
from src.sender import send_outreach
from src.storage import Store

ROOT = Path(__file__).resolve().parent.parent
RECENT = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
OLD = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d


def video(i, title, views, likes, comments, date=RECENT):
    return {"id": f"v{i}", "snippet": {"title": title, "publishedAt": date},
            "statistics": {"viewCount": str(views), "likeCount": str(likes), "commentCount": str(comments)}}


BEAUTY = ["My skincare routine for acne", "Drugstore makeup tutorial", "GRWM for a party", "Best serum review",
          "Haircare routine for curly hair", "Nail art ideas"]
GAMING = ["Elden Ring boss guide", "Top 10 FPS games", "Minecraft build", "Speedrun attempt"]
# id -> (title, subs, hidden, description, titles, country, vids)
CHANNELS = {
    "TEST_good": ("TEST_ Maya Chen", 25000, False, "Skincare & makeup. Business: test.maya@example.org IG: @test_maya",
                  BEAUTY, "US", RECENT),
    "TEST_noemail": ("TEST_ Fashion Fox", 40000, False, "Outfit ideas and thrift hauls.",
                     ["Spring outfit ideas", "Thrift haul try on", "Capsule wardrobe 2026", "Streetwear lookbook"], None, RECENT),
    "TEST_big": ("TEST_ Mega Glam", 900000, False, "makeup", BEAUTY, "US", RECENT),
    "TEST_hidden": ("TEST_ Hidden Subs", 0, True, "skincare", BEAUTY, "GB", RECENT),
    "TEST_gamer": ("TEST_ Pro Gamer", 30000, False, "gaming", GAMING, "US", RECENT),
    "TEST_dead": ("TEST_ Old Stylist", 20000, False, "outfit ideas", ["Outfit 1", "Outfit 2", "Outfit 3", "Outfit 4"], "US", OLD),
    "TEST_lowEng": ("TEST_ Low Engage", 30000, False, "makeup", BEAUTY, "US", RECENT),
}


class FakeSession:
    def get(self, url, params=None, timeout=None, headers=None):
        ep = url.rsplit("/", 1)[-1]
        if ep == "search":
            return Resp({"items": [{"id": {"channelId": c}} for c in CHANNELS]})
        if ep == "channels":
            items = []
            for cid in params["id"].split(","):
                t, subs, hidden, desc, _, country, _ = CHANNELS[cid]
                sn = {"title": t, "description": desc, "customUrl": f"@{cid.lower()}"}
                if country:
                    sn["country"] = country
                items.append({"id": cid, "snippet": sn,
                              "statistics": {"subscriberCount": str(subs), "hiddenSubscriberCount": hidden},
                              "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid}}, "brandingSettings": {"channel": {}}})
            return Resp({"items": items})
        if ep == "playlistItems":
            cid = params["playlistId"][2:]
            return Resp({"items": [{"contentDetails": {"videoId": f"{cid}|{i}"}} for i in range(len(CHANNELS[cid][4]))]})
        if ep == "videos":
            out = []
            for vid in params["id"].split(","):
                cid, i = vid.split("|")
                _, _, _, _, titles, _, date = CHANNELS[cid]
                likes = 20 if cid == "TEST_lowEng" else 400
                out.append(video(vid, titles[int(i)], 10000, likes, 40, date))
            return Resp({"items": out})
        return Resp({}, 404)


class FakeLLM:
    """Stands in for the Anthropic client. First call returns a too-short email to test the retry loop."""
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        user = json.loads(kw["messages"][0]["content"].split("\n\nYour previous")[0])
        s = user["influencer"]
        if self.calls == 1:
            out = {"subject": "Hi", "email_body": "Hi there, collab?", "instagram_dm": "hey"}
        else:
            out = {"subject": f"Collab idea for {s['name']}",
                   "email_body": (f"Hi {s['name']},\n\nYour {s['niche'].lower()} content, especially {s['recent_video_title'] or 'your latest upload'}, "
                                  f"really stands out. I think your audience would love {user['brand']}, {user['brand_description']}. "
                                  f"We would like to propose {s['collab_angle']}, offering {s['offer']}, with you keeping full creative control. "
                                  f"Would you be open to a short call this week to discuss details?\n\nBest,\n{user['sender_name']}\n{user['brand']}"),
                   "instagram_dm": f"Hi {s['name']}, really enjoyed your {s['niche'].lower()} content. Your audience looks perfect for {user['brand']}. Open to a collab chat?"}

        class B:  # mimic SDK block
            type, text = "text", f"```json\n{json.dumps(out)}\n```"
        return type("R", (), {"content": [B()]})()


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.cfg = load_config(str(ROOT / "config.yaml"))
        self.cfg["storage"]["db_path"] = ":memory:"
        self.cfg["sending"].update(outbox_dir=f"{t}/outbox", dm_queue_csv=f"{t}/dm.csv",
                                   suppression_file=f"{t}/supp.txt", delay_between_sends_s=0)
        self.cfg["enrichment"]["fetch_websites"] = False
        self.cfg["discovery"]["youtube"].update(queries=["x"], region_codes=[None], pages_per_query=1)
        self.store = Store(":memory:")
        client = YouTubeClient("fake-key", session=FakeSession())
        self.assertEqual(discover(self.cfg, self.store, "fake-key", client), len(CHANNELS))

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, cid):
        return self.store.get_influencer(f"youtube:{cid}")

    # ---- filtering ---------------------------------------------------------
    def test_filtering_pass_fail_with_reasons(self):
        run_filter(self.store, self.cfg)
        self.assertEqual(self.status("TEST_good")["status"], "shortlisted")
        self.assertEqual(self.status("TEST_noemail")["status"], "shortlisted")
        for cid, needle in [("TEST_big", "followers"), ("TEST_hidden", "unavailable"), ("TEST_gamer", "niche"),
                            ("TEST_dead", "days ago"), ("TEST_lowEng", "engagement")]:
            r = self.status(cid)
            self.assertEqual(r["status"], "rejected", cid)
            self.assertTrue(r["filter_summary"].startswith("FAIL"))
            self.assertIn(needle, r["filter_summary"], cid)

    # ---- enrichment: never guess emails ------------------------------------
    def test_enrichment_email_found_or_not_found(self):
        run_filter(self.store, self.cfg)
        stats = run_enrichment(self.store, self.cfg)
        self.assertEqual(stats["emails_found"], 1)
        good, none = self.status("TEST_good"), self.status("TEST_noemail")
        self.assertEqual(good["email"], "test.maya@example.org")
        self.assertEqual(good["email_source"], "channel_description")
        self.assertEqual(good["instagram_url"], "https://www.instagram.com/test_maya/")
        self.assertEqual(none["email"], NOT_FOUND)
        self.assertTrue(good["audience_age"].startswith("Not Available"))  # never invented

    def test_email_and_ig_extractors(self):
        self.assertEqual(find_emails("mail a@b.co, logo@2x.png, x@example.com"), ["a@b.co"])
        self.assertIsNone(find_instagram("see instagram.com/p/ABC123"))
        self.assertEqual(find_instagram("ig: @lily_x"), "https://www.instagram.com/lily_x/")

    def test_engagement_none_when_insufficient_data(self):
        self.assertIsNone(engagement_rate([video(1, "t", 100, 5, 1)]))
        self.assertIsNone(engagement_rate([{"statistics": {"viewCount": "100"}}] * 5))  # likes hidden

    # ---- personalization ---------------------------------------------------
    def _shortlist(self):
        run_filter(self.store, self.cfg)
        run_enrichment(self.store, self.cfg)

    def test_fallback_messages_meet_limits_for_all_inputs(self):
        self._shortlist()
        long_cfg = json.loads(json.dumps(self.cfg))
        long_cfg["campaign"]["brand_description"] = "a very long description of an extremely elaborate multi-category lifestyle goods company"
        for inf in self.store.influencers("shortlisted"):
            for c in (self.cfg, long_cfg):
                for i in range(30):  # vary id to hit every template branch
                    inf2 = {**inf, "id": f"{inf['id']}-{i}"}
                    sig = signals(inf2, c)
                    msg = fallback_generate(inf2, sig, c)
                    self.assertEqual(validate(msg, sig, c), [], msg)
        # no title / no themes edge case
        inf = {**self.store.influencers("shortlisted")[0], "recent_titles": "[]", "themes": "[]"}
        sig = signals(inf, self.cfg)
        self.assertEqual(validate(fallback_generate(inf, sig, self.cfg), sig, self.cfg), [])

    def test_messages_differ_between_influencers(self):
        self._shortlist()
        run_personalization(self.store, self.cfg, use_llm=False)
        bodies = {m["email_body"] for m in self.store.messages()}
        self.assertEqual(len(bodies), 2)

    def test_llm_retry_loop_then_accept(self):
        self._shortlist()
        llm = FakeLLM()
        stats = run_personalization(self.store, self.cfg, client=llm)
        self.assertEqual(stats["llm"], 2)
        self.assertGreater(llm.calls, 2)  # first attempt rejected -> retried
        for m in self.store.messages():
            self.assertTrue(60 <= words(m["email_body"]) <= 90)
            self.assertTrue(15 <= words(m["instagram_dm"]) <= 30)

    # ---- sending -----------------------------------------------------------
    def _ready(self):
        self._shortlist()
        run_personalization(self.store, self.cfg, use_llm=False)

    def test_unapproved_not_sent(self):
        self._ready()
        self.assertEqual(send_outreach(self.store, self.cfg)["skipped_not_approved"], 2)

    def test_dry_run_dedupe_and_log(self):
        self._ready()
        self.store.approve()
        s1 = send_outreach(self.store, self.cfg)
        self.assertEqual((s1["simulated"], s1["skipped_no_email"], s1["dm_queued"]), (1, 1, 2))
        s2 = send_outreach(self.store, self.cfg)  # second run must not duplicate anything
        self.assertEqual((s2["simulated"], s2["skipped_duplicate"], s2["dm_queued"]), (0, 1, 0))
        rows = self.store.outreach_rows()
        self.assertEqual(sum(r["status"] == "simulated_sent" for r in rows), 1)
        self.assertEqual(len(list(Path(self.cfg["sending"]["outbox_dir"]).glob("*.eml"))), 1)
        self.assertEqual(len(Path(self.cfg["sending"]["dm_queue_csv"]).read_text(encoding="utf-8").strip().splitlines()), 3)  # header + 2

    def test_suppression_list(self):
        self._ready()
        self.store.approve()
        Path(self.cfg["sending"]["suppression_file"]).write_text("TEST.maya@example.org\n")
        self.assertEqual(send_outreach(self.store, self.cfg)["skipped_suppressed"], 1)

    def test_live_send_uses_smtp_and_records_failure(self):
        self._ready()
        self.store.approve()

        class OkSMTP:
            sent = []
            def send_message(self, m): self.sent.append(m)
            def quit(self): pass
        smtp = OkSMTP()
        self.assertEqual(send_outreach(self.store, self.cfg, live=True, smtp=smtp)["sent"], 1)
        self.assertIn("unsubscribe", smtp.sent[0].get_content().lower())  # footer appended
        # a live send blocks any later send to the same address
        self.assertEqual(send_outreach(self.store, self.cfg, live=True, smtp=smtp)["skipped_duplicate"], 1)

    def test_live_send_failure_is_logged_not_raised(self):
        self._ready()
        self.store.approve()
        import smtplib

        class BadSMTP:
            def send_message(self, m): raise smtplib.SMTPRecipientsRefused({})
            def quit(self): pass
        st = send_outreach(self.store, self.cfg, live=True, smtp=BadSMTP())
        self.assertEqual(st["failed"], 1)
        self.assertTrue(any(r["status"] == "failed" for r in self.store.outreach_rows()))

    # ---- export ------------------------------------------------------------
    def test_exports(self):
        self._ready()
        self.store.approve()
        send_outreach(self.store, self.cfg)
        out = f"{self.tmp.name}/exp"
        export_all(self.store, out)
        for f in ("influencer_dataset.csv", "personalized_messages.csv", "outreach_tracker.csv"):
            self.assertTrue((Path(out) / f).stat().st_size > 0)
        ds = (Path(out) / "influencer_dataset.csv").read_text(encoding="utf-8-sig")
        self.assertIn("Not Found", ds)
        self.assertIn("Not Available", ds)


class CsvImportTest(unittest.TestCase):
    def test_csv_import_validation(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "c.csv"
            p.write_text("name,platform,profile_url,followers,engagement_rate,email,bio\n"
                         "TEST_ A,instagram,https://instagram.com/test_a,\"12,000\",3.1,,skincare and makeup\n"
                         "TEST_ B,instagram,,5000,,,\n"           # missing url -> skipped
                         "TEST_ C,tiktok,https://tiktok.com/@c,abc,,,\n", encoding="utf-8")
            s = Store(":memory:")
            self.assertEqual(import_csv(str(p), s), 2)  # C saved with followers=None, B skipped
            self.assertIsNone([i for i in s.influencers() if i["name"] == "TEST_ C"][0]["followers"])
            (Path(t) / "bad.csv").write_text("name,platform\nx,y\n")
            with self.assertRaises(ValueError):
                import_csv(str(Path(t) / "bad.csv"), s)


if __name__ == "__main__":
    unittest.main()
