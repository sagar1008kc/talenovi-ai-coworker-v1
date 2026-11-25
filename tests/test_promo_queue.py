"""Promo Shorts queue: metadata, ordering, 12-hour interval, render guards (no network, no ffmpeg)."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio import youtube_upload as yt  # noqa: E402
import promo_queue as pq  # noqa: E402

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
CFG = {"expected_channel_id": "UC" + "a" * 22, "category_id": "27", "contains_synthetic_media": True,
       "made_for_kids": False, "min_hours_between_uploads": 12, "privacy": "unlisted",
       "keyring_user": "promo-oauth-credentials", "campaigns": ["pilotmycareer"]}


class RealCampaignTests(unittest.TestCase):
    """The committed PilotMyCareer campaign must always produce valid YouTube metadata."""

    def setUp(self):
        self.posts = pq.posts([pq.load_campaign("pilotmycareer")])

    def test_every_post_is_valid(self):
        self.assertGreaterEqual(len(self.posts), 13)
        for p in self.posts:
            self.assertTrue(p.image.is_file(), p.image)
            self.assertEqual(yt.sha256_file(p.image), p.data["image_sha256"])
            plan = pq.make_plan(p, CFG)
            body = plan.body()
            self.assertEqual(body["status"]["privacyStatus"], "unlisted")
            self.assertTrue(body["status"]["containsSyntheticMedia"])
            self.assertFalse(body["status"]["selfDeclaredMadeForKids"])
            self.assertLessEqual(len(plan.title), yt.MAX_TITLE_CHARS)
            self.assertIn("https://www.pilotmycareer.com", plan.description)
            self.assertIn("AI-generated", plan.description)
            last = plan.description.splitlines()[-1].split()
            self.assertEqual(last[0], "#Shorts")
            self.assertLessEqual(len(last), yt.MAX_HASHTAGS)
            self.assertTrue(body["snippet"]["tags"])

    def test_images_are_unique(self):
        shas = [p.data["image_sha256"] for p in self.posts]
        self.assertEqual(len(shas), len(set(shas)))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        d = self.tmp / "promo" / "demo" / "images"
        d.mkdir(parents=True)
        for i in (1, 2):
            (d / f"{i}.png").write_bytes(b"img%d" % i)
        camp = {"campaign": "Demo", "slug": "demo", "site": "https://example.com",
                "description_template": "{hook}\n\n{audience}\n\n{site}\n\n{hashtags}",
                "base_hashtags": ["Shorts"], "base_tags": ["demo"],
                "posts": [{"id": i, "image": f"images/{i}.png", "image_sha256": yt.sha256_file(d / f"{i}.png"),
                           "title": f"T{i}", "hook": "H", "audience": "A", "hashtags": ["X"]} for i in (1, 2)]}
        (self.tmp / "promo" / "demo" / "campaign.json").write_text(json.dumps(camp))
        self.posts = pq.posts([pq.load_campaign("demo", self.tmp / "promo")])
        self.ledger = yt.Ledger(self.tmp / "ledger.json")
        self.halt = self.tmp / "HALT"

    def done(self, post_id: int, when: datetime):
        self.ledger.uploads[f"sha{post_id}"] = {"status": "done", "story_slug": "promo-demo", "episode": post_id,
                                                "uploaded_at": when.isoformat(), "video_id": "x" * 11}

    def test_order_and_twelve_hour_interval(self):
        d = pq.decide(self.ledger, self.posts, CFG, NOW, self.halt)
        self.assertEqual((d.action, d.post.id), ("upload", 1))
        self.done(1, NOW - timedelta(hours=11))
        d = pq.decide(self.ledger, self.posts, CFG, NOW, self.halt)
        self.assertEqual((d.action, d.post.id), ("wait", 2))
        self.assertEqual(d.due_at, NOW + timedelta(hours=1))
        d = pq.decide(self.ledger, self.posts, CFG, NOW + timedelta(hours=1), self.halt)
        self.assertEqual((d.action, d.post.id), ("upload", 2))
        self.done(2, NOW)
        self.assertEqual(pq.decide(self.ledger, self.posts, CFG, NOW + timedelta(days=1), self.halt).action, "idle")

    def test_halt_and_pending_block(self):
        self.ledger.uploads["p"] = {"status": "pending", "story_slug": "promo-demo", "episode": 1, "started_at": "t"}
        self.assertEqual(pq.decide(self.ledger, self.posts, CFG, NOW, self.halt).action, "halt")
        del self.ledger.uploads["p"]
        self.halt.write_text("stop")
        self.assertEqual(pq.decide(self.ledger, self.posts, CFG, NOW, self.halt).action, "halt")

    def test_metadata_and_public_refused(self):
        plan = pq.make_plan(self.posts[0], CFG)
        self.assertEqual(plan.description, "H\n\nA\n\nhttps://example.com\n\n#Shorts #X")
        self.assertEqual((plan.story_slug, plan.episode), ("promo-demo", 1))
        with self.assertRaises(yt.UploadError):
            pq.make_plan(self.posts[0], {**CFG, "privacy": "public"}).body()

    def test_changed_image_is_refused_before_render(self):
        self.posts[0].image.write_bytes(b"tampered")
        calls = []
        with self.assertRaises(yt.UploadError):
            pq.build(self.posts[0], runner=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_render_writes_atomically(self):
        def runner(args, **kw):
            Path(args[-1]).write_bytes(b"mp4")
            return SimpleNamespace(returncode=0, stderr="")
        out = pq.build(self.posts[1], runner=runner)
        self.assertEqual(out.read_bytes(), b"mp4")
        self.assertEqual(out.name, "demo_post02.mp4")
        self.assertEqual([f.name for f in out.parent.iterdir()], ["demo_post02.mp4"])

    def test_channel_must_be_pinned_and_music_needs_provenance(self):
        with self.assertRaises(yt.UploadError):
            pq.require_channel({"expected_channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx"[:10]})
        prov = self.tmp / "provenance.json"
        prov.write_text(json.dumps({"tracks": [{"file": "a.mp3", "commercial_use_confirmed": True,
                                                "platforms_confirmed": ["tiktok"]}]}))
        with self.assertRaises(yt.UploadError):
            pq.music_file({"video": {"audio_file": "a.mp3"}}, prov)
        self.assertIsNone(pq.music_file({"video": {}}, prov))

    def test_tag_limit(self):
        with self.assertRaises(yt.UploadError):
            yt.check_tags(["x" * 300, "y" * 300])
        self.assertEqual(yt.check_tags([" a ", "", "b c"]), ("a", "b c"))


if __name__ == "__main__":
    unittest.main()
