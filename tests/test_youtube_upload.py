"""YouTube uploader and daily queue: formatting, ordering, interval and duplicate safety (no network)."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio import youtube_upload as yt  # noqa: E402
import youtube_queue as q  # noqa: E402

CFG = {"expected_channel_id": "UCexpected", "category_id": "24", "contains_synthetic_media": True,
       "min_hours_between_uploads": 24, "default_hashtags": ["Shorts"]}
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


class FormattingTests(unittest.TestCase):
    def test_hashtags_exactly_one_hash(self):
        self.assertEqual(yt.normalize_hashtags("ShortStory, #Mystery,##Episode3, ,mystery"),
                         ["#ShortStory", "#Mystery", "#Episode3"])
        self.assertEqual(yt.normalize_hashtags(["Short Story", "#A"]), ["#ShortStory", "#A"])
        with self.assertRaises(yt.UploadError):
            yt.normalize_hashtags("a#b")
        with self.assertRaises(yt.UploadError):
            yt.normalize_hashtags(",".join(f"t{i}" for i in range(16)))

    def test_title_and_description(self):
        self.assertEqual(yt.build_title("The Spare Key", 3, "The Green Box"),
                         "The Spare Key | Episode 3 | The Green Box")
        self.assertEqual(yt.build_description(" Summary. ", ["#A", "#B"]), "Summary.\n\n#A #B")
        with self.assertRaises(yt.UploadError):
            yt.build_title("S", 1, "x" * 100)
        with self.assertRaises(yt.UploadError):
            yt.build_description("a <b>", [])

    def test_full_story_title(self):
        self.assertEqual(yt.build_full_story_title("The Spare Key"), "The Spare Key | Full Story")
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "v.mp4"
            v.write_bytes(b"x")
            plan = yt.make_plan(v, "The Spare Key", 0, "", "Sum", "a", config=CFG, full_story=True)
        self.assertEqual((plan.title, plan.story_slug, plan.episode),
                         ("The Spare Key | Full Story", "the-spare-key-full-story", 0))
        self.assertEqual(plan.body()["status"]["privacyStatus"], "unlisted")

    def test_body_is_always_unlisted(self):
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "v.mp4"
            v.write_bytes(b"x")
            body = yt.make_plan(v, "S", 1, "T", "Sum", "a", config=CFG).body()
        self.assertEqual(body["status"]["privacyStatus"], "unlisted")
        self.assertTrue(body["status"]["containsSyntheticMedia"])
        self.assertEqual(body["snippet"]["title"], "S | Episode 1 | T")


class FakeRequest:
    def __init__(self, response, fail_with=None, size=1):
        self.response, self.fail_with, self.resumable_progress, self.size = response, fail_with, 0, size

    def next_chunk(self):
        if self.fail_with:
            self.resumable_progress = self.size if self.fail_with == "after_all_bytes" else 0
            raise yt.UploadError("network gone")
        return None, self.response


class FakeYouTube:
    def __init__(self, req, channel="UCexpected"):
        self.req, self.bodies, self.channel = req, [], channel

    def videos(self):
        return self

    def channels(self):
        return self

    def list(self, part, mine):
        return self

    def execute(self):
        return {"items": [{"id": self.channel, "snippet": {"title": "Chan"}}]}

    def insert(self, part, body, media_body):
        self.bodies.append(body)
        return self.req


class UploadLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.video = self.dir / "s_episode01.mp4"
        self.video.write_bytes(b"video-bytes")
        self.ledger = self.dir / "ledger.json"
        self.plan = yt.make_plan(self.video, "S", 1, "T", "Sum", "a", story_slug="s", config=CFG)

    def tearDown(self):
        self.tmp.cleanup()

    def _upload(self, req, channel="UCexpected"):
        fake = FakeYouTube(req, channel)
        with mock.patch("googleapiclient.http.MediaFileUpload"):
            return yt.upload(self.plan, interactive=False, ledger_path=self.ledger, config=CFG,
                             service_factory=lambda: fake), fake

    def test_success_records_and_blocks_duplicate(self):
        resp = {"id": "abcdefghijk", "status": {"privacyStatus": "unlisted"},
                "snippet": {"channelId": "UCexpected", "channelTitle": "Chan"}}
        result, fake = self._upload(FakeRequest(resp))
        self.assertEqual(result.privacy, "unlisted")
        self.assertEqual(result.warnings, ())
        self.assertEqual(fake.bodies[0]["status"]["privacyStatus"], "unlisted")
        entry = yt.Ledger(self.ledger).uploads[hashlib.sha256(b"video-bytes").hexdigest()]
        self.assertEqual((entry["status"], entry["video_id"]), ("done", "abcdefghijk"))
        with self.assertRaises(yt.DuplicateUpload):
            self._upload(FakeRequest(resp))
        # Same story/episode under a different file is also refused.
        self.video.write_bytes(b"re-encoded")
        plan2 = yt.make_plan(self.video, "S", 1, "T", "Sum", "a", story_slug="s", config=CFG)
        self.plan = plan2
        with self.assertRaises(yt.DuplicateUpload):
            self._upload(FakeRequest(resp))

    def test_replace_previous_needs_a_new_file(self):
        resp = {"id": "abcdefghijk", "status": {"privacyStatus": "unlisted"}, "snippet": {"channelId": "UCexpected"}}
        self._upload(FakeRequest(resp))
        fake = FakeYouTube(FakeRequest(resp))
        with mock.patch("googleapiclient.http.MediaFileUpload"), self.assertRaises(yt.DuplicateUpload):
            yt.upload(self.plan, interactive=False, ledger_path=self.ledger, config=CFG,
                      service_factory=lambda: fake, replace_previous=True)  # same file: still refused
        self.video.write_bytes(b"new widescreen render")
        self.plan = yt.make_plan(self.video, "S", 1, "T", "Sum", "a", story_slug="s", config=CFG)
        with mock.patch("googleapiclient.http.MediaFileUpload"):
            yt.upload(self.plan, interactive=False, ledger_path=self.ledger, config=CFG,
                      service_factory=lambda: fake, replace_previous=True)
        statuses = sorted(e["status"] for e in yt.Ledger(self.ledger).uploads.values())
        self.assertEqual(statuses, ["done", "replaced"])

    def test_private_or_wrong_channel_warns(self):
        resp = {"id": "abcdefghijk", "status": {"privacyStatus": "private"}, "snippet": {"channelId": "UCother"}}
        result, _ = self._upload(FakeRequest(resp))
        self.assertEqual(len(result.warnings), 2)

    def test_wrong_channel_refused_before_any_bytes(self):
        with self.assertRaises(yt.WrongChannel):
            self._upload(FakeRequest({"id": "x"}), channel="UCother")
        self.assertEqual(yt.Ledger(self.ledger).uploads, {})

    def test_wrong_channel_upload_does_not_count_as_published(self):
        resp = {"id": "abcdefghijk", "status": {"privacyStatus": "unlisted"}, "snippet": {"channelId": "UCother"}}
        result, _ = self._upload(FakeRequest(resp))  # sign-in check passed, but YouTube reports another channel
        self.assertEqual(len(result.warnings), 1)
        entry = list(yt.Ledger(self.ledger).uploads.values())[0]
        self.assertEqual(entry["status"], "wrong_channel")
        self._upload(FakeRequest({**resp, "snippet": {"channelId": "UCexpected"}}))  # re-upload allowed

    def test_incomplete_upload_is_retryable(self):
        with self.assertRaises(yt.UploadError):
            self._upload(FakeRequest(None, fail_with="early"))
        self.assertEqual(list(yt.Ledger(self.ledger).uploads.values())[0]["status"], "failed")
        resp = {"id": "abcdefghijk", "status": {"privacyStatus": "unlisted"}, "snippet": {"channelId": "UCexpected"}}
        self._upload(FakeRequest(resp))  # retry allowed

    def test_unconfirmed_upload_stays_pending_and_blocks(self):
        with self.assertRaises(yt.UploadError):
            self._upload(FakeRequest(None, fail_with="after_all_bytes", size=len(b"video-bytes")))
        self.assertEqual(len(yt.Ledger(self.ledger).pending()), 1)
        with self.assertRaises(yt.DuplicateUpload):
            self._upload(FakeRequest({"id": "x"}))


class WidescreenTests(unittest.TestCase):
    def test_shot_focus(self):
        from studio.widescreen import shot_focus
        self.assertAlmostEqual(shot_focus({"shot_size": "close"}), 0.32)
        self.assertAlmostEqual(shot_focus({"shot_size": "insert", "caption_zone": "high"}), 0.65)
        self.assertLessEqual(shot_focus({"shot_size": "wide", "caption_zone": "high"}), 0.75)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.up = Path(self.tmp.name) / "uploads"
        self.halt = Path(self.tmp.name) / "HALT"
        for slug, n in [("alpha", 3), ("beta-two", 2)]:
            (self.up / slug).mkdir(parents=True)
            for i in range(1, n + 1):
                (self.up / slug / f"{slug.replace('-', '_')}_episode{i:02d}.mp4").write_bytes(f"{slug}{i}".encode())
        (self.up / "alpha" / "wrong_episode04.mp4").write_bytes(b"x")
        self.ledger = yt.Ledger(Path(self.tmp.name) / "ledger.json")

    def tearDown(self):
        self.tmp.cleanup()

    def mark(self, slug, ep, when, status="done"):
        self.ledger.uploads[f"{slug}{ep}"] = {"status": status, "story_slug": slug, "episode": ep,
                                              "uploaded_at": when.isoformat(), "started_at": when.isoformat()}

    def decide(self, **kw):
        stories = q.discover(self.up)
        order = q.update_story_order(self.ledger, stories, self.up)
        return q.decide(self.ledger, stories, order, CFG, NOW, halt_path=self.halt, **kw)

    def test_discover_ignores_misnamed(self):
        stories = q.discover(self.up)
        self.assertEqual(sorted(stories["alpha"]), [1, 2, 3])
        self.assertEqual(sorted(stories["beta-two"]), [1, 2])

    def test_order_episodes_then_next_story(self):
        self.ledger.data["story_order"] = ["alpha", "beta-two"]
        d = self.decide()
        self.assertEqual((d.action, d.slug, d.episode), ("upload", "alpha", 1))
        old = NOW - timedelta(hours=25)
        for ep in (1, 2, 3):
            self.mark("alpha", ep, old)
        d = self.decide()
        self.assertEqual((d.action, d.slug, d.episode), ("upload", "beta-two", 1))
        for ep in (1, 2):
            self.mark("beta-two", ep, old)
        self.assertEqual(self.decide().action, "idle")

    def test_never_skips_a_missing_episode(self):
        self.ledger.data["story_order"] = ["alpha", "beta-two"]
        (self.up / "alpha" / "alpha_episode02.mp4").unlink()
        self.mark("alpha", 1, NOW - timedelta(hours=30))
        d = self.decide()
        self.assertEqual((d.slug, d.episode), ("beta-two", 1))  # alpha waits for 02; 03 is not uploaded early

    def test_24_hour_gate(self):
        self.ledger.data["story_order"] = ["alpha"]
        self.mark("alpha", 1, NOW - timedelta(hours=23, minutes=59))
        d = self.decide()
        self.assertEqual((d.action, d.episode), ("wait", 2))
        self.assertEqual(self.decide(ignore_interval=True).action, "upload")
        self.ledger.uploads.clear()
        self.mark("alpha", 1, NOW - timedelta(hours=24))
        self.assertEqual(self.decide().action, "upload")

    def test_pending_and_halt_stop_everything(self):
        self.mark("alpha", 1, NOW - timedelta(days=3), status="pending")
        self.assertEqual(self.decide().action, "halt")
        self.ledger.uploads.clear()
        self.halt.write_text("x")
        self.assertEqual(self.decide().action, "halt")

    def test_failed_attempt_is_retried(self):
        self.ledger.data["story_order"] = ["alpha"]
        self.mark("alpha", 1, NOW - timedelta(hours=1), status="failed")
        d = self.decide()
        self.assertEqual((d.action, d.episode), ("upload", 1))

    def test_new_story_order_is_remembered(self):
        stories = q.discover(self.up)
        order = q.update_story_order(self.ledger, stories, self.up)
        self.assertEqual(set(order), {"alpha", "beta-two"})
        (self.up / "gamma").mkdir()
        (self.up / "gamma" / "gamma_episode01.mp4").write_bytes(b"g")
        order2 = q.update_story_order(self.ledger, q.discover(self.up), self.up)
        self.assertEqual(order2[:2], order[:2])
        self.assertEqual(order2[2], "gamma")


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.up, self.series = root / "uploads", root / "series"
        (self.up / "tale").mkdir(parents=True)
        self.video = self.up / "tale" / "tale_episode01.mp4"
        self.video.write_bytes(b"master")
        ep = self.series / "tale" / "episodes" / "01"
        ep.mkdir(parents=True)
        (self.series / "tale" / "series_bible.json").write_text(json.dumps({"title": "The Tale"}))
        (ep / "story.json").write_text(json.dumps({"youtube_description": "Desc.", "tags": ["family mystery"]}))
        self.release = ep / "release.json"
        self.write_release(hashlib.sha256(b"master").hexdigest(), True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_release(self, sha, passed):
        self.release.write_text(json.dumps({"title": "Opening | The Tale, Part 1", "qc": {"passed": passed},
                                            "master": {"sha256": sha}}))

    def meta(self):
        return q.episode_metadata("tale", 1, self.video, CFG, uploads_dir=self.up, series_dir=self.series)

    def test_from_release(self):
        m = self.meta()
        self.assertEqual((m["story"], m["title"], m["summary"]), ("The Tale", "Opening", "Desc."))
        self.assertEqual(m["hashtags"], ["#Shorts", "#TheTale", "#Episode1", "#FamilyMystery"])

    def test_refuses_mismatched_or_failed_master(self):
        self.write_release("0" * 64, True)
        with self.assertRaises(yt.UploadError):
            self.meta()
        self.write_release(hashlib.sha256(b"master").hexdigest(), False)
        with self.assertRaises(yt.UploadError):
            self.meta()

    def test_sidecar_overrides_and_required_without_release(self):
        self.release.unlink()
        with self.assertRaises(yt.UploadError):
            self.meta()
        (self.up / "tale" / "metadata.json").write_text(json.dumps(
            {"story": "Tale", "episodes": {"01": {"title": "T1", "description": "D1", "hashtags": ["x"]}}}))
        m = self.meta()
        self.assertEqual((m["story"], m["title"], m["summary"], m["hashtags"]), ("Tale", "T1", "D1", ["#x"]))


if __name__ == "__main__":
    unittest.main()
