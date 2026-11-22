"""Mocked provider tests: payload schemas, async jobs, persistence, retries, timeouts, budgets."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image

from fakes import MP4_BYTES, FakeHttp, Resp, make_ledger
from studio.budget import BudgetExceeded
from studio.providers import build_video_providers, load_catalog
from studio.providers.base import (JobStore, ProviderError, RetryPolicy, VideoGenerationProvider, VideoRequest,
                                   generate_clip, run_job)
from studio.providers.fal import FalQueue
from studio.providers.runway import RunwayTasks

ROOT = Path(__file__).resolve().parents[1]
CATALOG = load_catalog(ROOT)
FAST = RetryPolicy(poll_initial_s=0, poll_max_s=0, base_delay_s=0, max_delay_s=0, timeout_s=600)
NOSLEEP = lambda _s: None  # noqa: E731


def fal_routes(status_sequence, result=None, submit=None, video=MP4_BYTES):
    return [
        ("POST", "queue.fal.run", submit or Resp(200, {"request_id": "req-1",
                                                     "status_url": "https://queue.fal.run/x/requests/req-1/status",
                                                     "response_url": "https://queue.fal.run/x/requests/req-1",
                                                     "cancel_url": "https://queue.fal.run/x/requests/req-1/cancel"})),
        ("GET", "/status", [Resp(200, {"status": s}) if isinstance(s, str) else s for s in status_sequence]),
        ("PUT", "/cancel", Resp(202, {})),
        ("GET", "requests/req-1", result or Resp(200, {"video": {"url": "https://cdn.fal.media/out.mp4"}})),
        ("GET", "cdn.fal.media", Resp(200, body=video)),
    ]


class ProviderTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.image = self.dir / "still.png"
        Image.new("RGB", (64, 112), (10, 20, 30)).save(self.image)
        self.ledger = make_ledger(self.dir)
        self.store = JobStore(self.dir / "jobs.json")

    def provider(self, key, http, adapter=None):
        from studio.providers import model_spec
        spec = model_spec(CATALOG, key)
        adapter = adapter or FalQueue(http, "test-key")
        return VideoGenerationProvider(spec, adapter, lambda p: "data:image/jpeg;base64,AAAA")

    def req(self, seconds=6.2, aspect="9:16"):
        return VideoRequest(shot_id="S00-01", mode="image_to_video", prompt="Waves at dawn",
                            image_path=self.image, seconds_needed=seconds, aspect_ratio=aspect, camera="zoom_in")


class PayloadTests(ProviderTestBase):
    def test_wan3_payload_uses_verified_fields(self):
        payload = self.provider("wan3_i2v", FakeHttp([])).build_payload(self.req())
        self.assertEqual(payload["start_image_url"], "data:image/jpeg;base64,AAAA")
        self.assertEqual(payload["duration"], 7)  # ceil(6.2)
        self.assertEqual(payload["resolution"], "720p")
        self.assertEqual(payload["aspect_ratio"], "9:16")
        self.assertFalse(payload["audio"])
        self.assertIn("dolly-in", payload["prompt"])

    def test_veo_duration_suffix_and_rounding_up(self):
        payload = self.provider("veo31_fast_i2v", FakeHttp([])).build_payload(self.req(seconds=4.5))
        self.assertEqual(payload["duration"], "6s")
        self.assertEqual(payload["image_url"], "data:image/jpeg;base64,AAAA")
        self.assertFalse(payload["generate_audio"])

    def test_seedance_string_duration_and_auto_aspect(self):
        payload = self.provider("seedance25_i2v", FakeHttp([])).build_payload(self.req(seconds=2))
        self.assertEqual(payload["duration"], "4")  # minimum allowed value
        self.assertEqual(payload["aspect_ratio"], "auto")

    def test_kling_has_no_aspect_parameter(self):
        payload = self.provider("kling4_i2v", FakeHttp([])).build_payload(self.req())
        self.assertNotIn("aspect_ratio", payload)
        self.assertEqual(payload["start_image_url"], "data:image/jpeg;base64,AAAA")

    def test_flux3max_caps_duration_at_max(self):
        provider = self.provider("flux3max_i2v", FakeHttp([]))
        self.assertEqual(provider.build_payload(self.req(seconds=40))["duration"], 20)
        self.assertAlmostEqual(provider.estimate_usd(self.req(seconds=5)), 5 * 0.17)

    def test_runway_ratio_mapping(self):
        provider = self.provider("runway_gen45_i2v", FakeHttp([]),
                                 adapter=RunwayTasks(FakeHttp([]), "secret", "https://api.dev.runwayml.com", "2024-11-06"))
        payload = provider.build_payload(self.req(aspect="16:9"))
        self.assertEqual(payload["ratio"], "1280:720")

    def test_providers_skip_missing_credentials(self):
        built = build_video_providers(CATALOG, ["wan3_i2v", "runway_gen45_i2v"], FakeHttp([]), env={"FAL_KEY": "k"})
        self.assertEqual([p.name for p in built], ["wan3_i2v"])
        self.assertEqual(build_video_providers(CATALOG, ["wan3_i2v"], FakeHttp([]), env={}), [])


class JobTests(ProviderTestBase):
    def test_submit_poll_download_persists_request_id(self):
        http = FakeHttp(fal_routes(["IN_QUEUE", "IN_PROGRESS", "COMPLETED"]))
        provider = self.provider("wan3_i2v", http)
        dest = self.dir / "clip.mp4"
        _, record = generate_clip([provider], self.req(), http=http, store=self.store, ledger=self.ledger,
                                  dest=dest, policy=FAST, sleep=NOSLEEP)
        self.assertEqual(record.request_id, "req-1")
        self.assertEqual(record.status, "downloaded")
        self.assertEqual(dest.read_bytes(), MP4_BYTES)
        self.assertEqual(JobStore(self.dir / "jobs.json").get("clip:S00-01:wan3_i2v").request_id, "req-1")
        entry = self.ledger.entries[0]
        self.assertEqual(entry.status, "ok")
        self.assertAlmostEqual(entry.actual_usd, 7 * 0.10)

    def test_resume_polls_without_resubmitting(self):
        http = FakeHttp(fal_routes(["IN_PROGRESS"]))
        provider = self.provider("wan3_i2v", http)
        never_stop = iter([False, True])
        with self.assertRaises(KeyboardInterrupt):  # simulate interruption after submission
            generate_clip([provider], self.req(), http=http, store=self.store, ledger=self.ledger,
                          dest=self.dir / "c.mp4", policy=FAST, sleep=NOSLEEP, should_stop=lambda: next(never_stop))
        self.assertEqual(http.count("POST"), 1)
        http2 = FakeHttp(fal_routes(["COMPLETED"]))
        provider2 = self.provider("wan3_i2v", http2)
        generate_clip([provider2], self.req(), http=http2, store=JobStore(self.dir / "jobs.json"),
                      ledger=self.ledger, dest=self.dir / "c.mp4", policy=FAST, sleep=NOSLEEP)
        self.assertEqual(http2.count("POST"), 0, "must not pay for a second submission")
        self.assertTrue((self.dir / "c.mp4").is_file())

    def test_retryable_submit_error_backs_off_then_succeeds(self):
        ok = Resp(200, {"request_id": "req-1", "status_url": "https://queue.fal.run/x/requests/req-1/status",
                        "response_url": "https://queue.fal.run/x/requests/req-1"})
        http = FakeHttp(fal_routes(["COMPLETED"], submit=[Resp(503, text="busy"), Resp(429, text="slow"), ok]))
        delays = []
        generate_clip([self.provider("wan3_i2v", http)], self.req(), http=http, store=self.store,
                      ledger=self.ledger, dest=self.dir / "c.mp4", policy=FAST, sleep=delays.append)
        self.assertEqual(http.count("POST"), 3)
        self.assertEqual(len(delays), 2)

    def test_non_retryable_error_falls_back_to_next_provider(self):
        http = FakeHttp(fal_routes(["COMPLETED"]))
        bad = FakeHttp([("POST", "queue.fal.run", Resp(422, text="invalid input"))])
        first, second = self.provider("wan3_i2v", bad), self.provider("veo31_fast_i2v", http)
        provider, record = generate_clip([first, second], self.req(), http=http, store=self.store,
                                         ledger=self.ledger, dest=self.dir / "c.mp4", policy=FAST, sleep=NOSLEEP)
        self.assertEqual(provider.name, "veo31_fast_i2v")
        self.assertEqual(bad.count("POST"), 1)
        self.assertEqual(self.ledger.entries[0].status, "failed_unbilled")
        self.assertEqual(self.ledger.video_total(), self.ledger.entries[1].counted_usd)

    def test_provider_reported_failure_then_fallback(self):
        failing = FakeHttp(fal_routes([Resp(200, {"status": "COMPLETED", "error": "NSFW", "error_type": "safety"})]))
        good = FakeHttp(fal_routes(["COMPLETED"]))
        provider, _ = generate_clip([self.provider("wan3_i2v", failing), self.provider("kling4_i2v", good)],
                                    self.req(), http=good, store=self.store, ledger=self.ledger,
                                    dest=self.dir / "c.mp4", policy=FAST, sleep=NOSLEEP)
        self.assertEqual(provider.name, "kling4_i2v")
        self.assertEqual(self.store.get("clip:S00-01:wan3_i2v").status, "failed")

    def test_timeout_cancels_and_counts_cost_conservatively(self):
        http = FakeHttp(fal_routes(["IN_PROGRESS"]))
        import itertools
        clock = itertools.chain([0], itertools.repeat(10_000))  # submit at t=0, then far past deadline
        with self.assertRaises(ProviderError):
            run_job(adapter=FalQueue(http, "k"), http=http, store=self.store, ledger=self.ledger, key="k1",
                    model_id="alibaba/wan-3.0/image-to-video", payload={"prompt": "x"}, estimated_usd=0.5,
                    item="clip", dest=self.dir / "c.mp4", kind="video",
                    policy=RetryPolicy(timeout_s=60, poll_initial_s=0, poll_max_s=0), sleep=NOSLEEP,
                    now=lambda: next(clock))
        self.assertEqual(http.count("PUT", "/cancel"), 1)
        self.assertEqual(self.store.get("k1").status, "timeout")
        self.assertEqual(self.ledger.entries[0].status, "possibly_billed")
        self.assertAlmostEqual(self.ledger.video_total(), 0.5)

    def test_budget_refusal_happens_before_any_http_call(self):
        http = FakeHttp(fal_routes(["COMPLETED"]))
        ledger = make_ledger(self.dir / "b", video=0.2)
        with self.assertRaises(BudgetExceeded):
            generate_clip([self.provider("wan3_i2v", http)], self.req(), http=http, store=self.store,
                          ledger=ledger, dest=self.dir / "c.mp4", policy=FAST, sleep=NOSLEEP)
        self.assertEqual(http.calls, [])

    def test_download_rejects_non_video_bytes(self):
        http = FakeHttp(fal_routes(["COMPLETED"], video=b"<html>error</html>"))
        with self.assertRaises(ProviderError):
            generate_clip([self.provider("wan3_i2v", http)], self.req(), http=http, store=self.store,
                          ledger=self.ledger, dest=self.dir / "c.mp4", policy=FAST, sleep=NOSLEEP)
        self.assertFalse((self.dir / "c.mp4").exists())

    def test_runway_task_lifecycle(self):
        http = FakeHttp([
            ("POST", "/v1/image_to_video", Resp(200, {"id": "task-9"})),
            ("GET", "/v1/tasks/task-9", [Resp(200, {"status": "THROTTLED"}), Resp(200, {"status": "RUNNING"}),
                                         Resp(200, {"status": "SUCCEEDED", "output": ["https://runway.example/o.mp4"]})]),
            ("GET", "runway.example", Resp(200, body=MP4_BYTES)),
        ])
        adapter = RunwayTasks(http, "secret", "https://api.dev.runwayml.com", "2024-11-06")
        provider = self.provider("runway_gen45_i2v", http, adapter=adapter)
        _, record = generate_clip([provider], self.req(aspect="16:9"), http=http, store=self.store,
                                  ledger=self.ledger, dest=self.dir / "r.mp4", policy=FAST, sleep=NOSLEEP)
        body = http.calls[0][2]["json"]
        self.assertEqual(body["model"], "gen4.5")
        self.assertEqual(body["ratio"], "1280:720")
        self.assertEqual(http.calls[0][2]["headers"]["X-Runway-Version"], "2024-11-06")
        self.assertEqual(record.request_id, "task-9")


if __name__ == "__main__":
    unittest.main()
