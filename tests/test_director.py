"""Director pipeline: bible lock, shotlist state machine, targeted retries with reuse, budget stop,
16:9 quality guard, audio graph, upload gate. All offline (dry-run placeholders, no network)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

from pydantic import ValidationError

from studio.director import assemble, qa
from studio.director.bible import BibleError, load_bible
from studio.director.pipeline import Director
from studio.director.schema import Shot
from studio.director.shotlist import (RetryLimitReached, ShotStateError, check_shotlist, load_shotlist,
                                      redo_parts, retry, transition)
from studio.director.spend import SpendCapExceeded
from tests.director_fixture import DEMO_ENV, build_demo_show, demo_shots


def small_format(root: Path, shots: int = 3, seconds: float = 4.0) -> None:
    cfg_path = root / "config" / "studio.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["director"]["episode_format"].update(min_seconds=8, max_seconds=40, target_seconds=12, min_shots=2,
                                             max_shots=6)
    cfg_path.write_text(json.dumps(cfg))
    sl_path = root / "shows" / "demo-show" / "episodes" / "ep01" / "shotlist.json"
    sl = json.loads(sl_path.read_text())
    sl["shots"] = demo_shots(shots, seconds)
    sl_path.write_text(json.dumps(sl))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_demo_show(Path(self.tmp.name))
        self.show = self.root / "shows" / "demo-show"
        self.ep = self.show / "episodes" / "ep01"
        self.cfg = json.loads((self.root / "config" / "studio.json").read_text())["director"]

    def tearDown(self) -> None:
        self.tmp.cleanup()


class SchemaAndShotlist(Base):
    def test_dialogue_needs_speaker_and_voice(self):
        with self.assertRaises(ValidationError):
            Shot(shot_id="S01", duration_sec=4, location_id="orchard", camera_movement="push_in", action="x",
                 dialogue_line="Hello there")
        with self.assertRaises(ValidationError):
            Shot(shot_id="S01", duration_sec=4, location_id="orchard", camera_movement="push_in", action="x",
                 lip_sync_required=True)

    def test_demo_shotlist_is_valid(self):
        errors, _ = check_shotlist(load_shotlist(self.ep), load_bible(self.show), self.cfg["episode_format"])
        self.assertEqual(errors, [])

    def test_wrong_voice_and_runtime_are_errors(self):
        sl = load_shotlist(self.ep)
        sl.shots[1].voice_id = "ELEVENLABS_DEMO_B_VOICE_ID"   # pip speaks S02, B is bram's voice
        sl.shots = sl.shots[:10]
        errors, _ = check_shotlist(sl, load_bible(self.show), self.cfg["episode_format"])
        self.assertTrue(any("not pip's locked voice" in e for e in errors))
        self.assertTrue(any("runtime" in e for e in errors))
        self.assertTrue(any("10 shots" in e for e in errors))


class BibleLock(Base):
    def test_changed_reference_and_voice_are_detected(self):
        bible = load_bible(self.show)
        self.assertEqual(bible.verify(DEMO_ENV), [])
        self.assertTrue(bible.verify({**DEMO_ENV, "ELEVENLABS_DEMO_A_VOICE_ID": "other"}))
        ref = bible.character_refs("pip")[0]
        ref.write_bytes(ref.read_bytes() + b"x")
        problems = load_bible(self.show).verify(None)
        self.assertTrue(any("pip:ref:front.png" in p for p in problems))
        with self.assertRaises(BibleError):
            load_bible(self.show).lock(DEMO_ENV, "test", extend=True)


class StateMachine(Base):
    def test_transitions_retry_cap_and_redo(self):
        shot = load_shotlist(self.ep).shots[1]
        with self.assertRaises(ShotStateError):
            transition(shot, "approved")
        shot.attempts = 1
        transition(shot, "rendered")
        shot.qa = {"checks": [qa.check("stt_dialogue", False, "x")]}
        transition(shot, "qa_failed")
        self.assertEqual(redo_parts(shot), ["lipsync", "voice"])
        shot.assets["current"] = {"video": "a.mp4"}
        retry(shot, 2, "t")
        self.assertEqual(shot.status, "pending")
        self.assertEqual(shot.assets["rejected"][-1]["redo"], ["lipsync", "voice"])
        shot.attempts = 3
        transition(shot, "qa_failed")
        with self.assertRaises(RetryLimitReached):
            retry(shot, 2, "t")

    def test_word_error_rate(self):
        self.assertEqual(qa.word_error_rate("You kept the lantern lit.", "you kept the lantern lit"), 0.0)
        self.assertAlmostEqual(qa.word_error_rate("a b c d", "a x c d"), 0.25)


class BudgetStop(Base):
    def test_estimate_over_cap_stops_before_any_call(self):
        env = {**DEMO_ENV, "FAL_KEY": "k", "ELEVENLABS_API_KEY": "k"}
        d = Director(self.root, self.show, self.ep, env=env, log=lambda *_: None, http=object())
        with self.assertRaises(SpendCapExceeded):
            d.produce(confirm_spend=True)
        alert = json.loads((self.ep / "reports" / "ALERT.json").read_text())
        self.assertEqual(alert["kind"], "budget")
        self.assertGreater(alert["estimate"]["fal_usd_first_pass"], 6)
        self.assertFalse((self.ep / "shots").exists())

    def test_paid_run_needs_consent(self):
        d = Director(self.root, self.show, self.ep, env={}, log=lambda *_: None, http=object())
        with self.assertRaises(Exception):
            d.produce(confirm_spend=False)


class Reframe(unittest.TestCase):
    def _plan(self, w, h, faces):
        shot = Shot(shot_id="S01", duration_sec=4, location_id="orchard", camera_movement="push_in", action="x")
        probe = {"streams": [{"codec_type": "video", "width": w, "height": h}], "format": {"duration": "4"}}
        with mock.patch.object(assemble.vision, "probe", return_value=probe), \
                mock.patch.object(assemble.vision, "duration", return_value=4.0), \
                mock.patch.object(assemble.vision, "extract_frames", return_value=[Path("f.png")]), \
                mock.patch.object(assemble.vision, "faces", return_value={"f.png": faces}):
            return assemble.reframe_plan(Path("x.mp4"), shot, Path("."), 1920, 1080, 1.5)

    def test_quality_guard_never_upscales_past_limit(self):
        p = self._plan(720, 1280, [[0.4, 0.3, 0.2, 0.1]])
        self.assertEqual(p["layout"], "framed")          # 720p cannot fill 1920 wide sharply
        self.assertLessEqual(p["upscale"], 1.5)
        p = self._plan(1440, 2560, [[0.4, 0.3, 0.2, 0.1]])
        self.assertEqual(p["layout"], "fill")            # 4K-class source fills the band
        self.assertLessEqual(p["top"], 0.3)
        p = self._plan(720, 1280, [[0.1, 0.05, 0.2, 0.1], [0.6, 0.8, 0.2, 0.1]])
        self.assertEqual(p["layout"], "full")            # faces spread over the frame: never cut

    def test_mix_graph_ducks_and_normalizes(self):
        a = json.loads(Path("config/studio.json").read_text())["director"]["audio"]
        g = assemble.mix_graph(2, True, 90.0, a)
        self.assertEqual(g.count("sidechaincompress"), 2)
        self.assertIn(f"loudnorm=I={a['master_lufs']}", g)
        self.assertIn("amix=inputs=5", g)


class EndToEndDryRun(Base):
    def test_produce_reject_one_shot_and_reuse(self):
        small_format(self.root)
        d = Director(self.root, self.show, self.ep, dry_run=True, env={}, log=lambda *_: None)
        state = d.produce(confirm_spend=False)
        self.assertTrue(state["all_approved"])
        qa_result = json.loads((d.episode / "reports" / "episode_qa.json").read_text())
        self.assertTrue(qa_result["passed"], [c for c in qa_result["checks"] if not c["passed"]])
        before = {s.shot_id: s.assets["current"]["take"] for s in d.sl.shots}
        d.reject_shot("S02", "line read too flat", redo=["voice"])
        d.produce(confirm_spend=False)
        after = {s.shot_id: s for s in d.sl.shots}
        self.assertEqual(after["S02"].attempts, 2)
        self.assertIn("motion", after["S02"].assets["current"]["reused"])
        for sid in ("S01", "S03"):
            self.assertEqual(after[sid].assets["current"]["take"], before[sid])   # untouched
        wide = d.wide()
        self.assertTrue((d.episode / "master_16x9.mp4").is_file())
        self.assertIn("framed", wide["layouts"])                                    # 720p source


def speech_spans(path: Path) -> list[tuple[float, float]]:
    import re, subprocess
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=n=-35dB:d=0.1", "-f",
                          "null", "-"], capture_output=True, text=True).stderr
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return [(e, e) for e in ends]               # each silence end = a speech onset


class AudioPlacement(unittest.TestCase):
    """Regression: `adelay` followed by `apad,atrim` truncated delayed audio (pilot 2026-11-22)."""

    def test_performance_alignment_places_phrases_on_the_performance(self):
        from studio.director import sync
        from studio.director.steps import fake_voice
        with tempfile.TemporaryDirectory() as tmp:
            tts = Path(tmp) / "line.mp3"
            fake_voice(tts, "I know. I am still trying.")
            al = sync.load_alignment(tts)
            perf = [("I", 2.2, 2.3), ("know.", 2.32, 2.55), ("I", 3.0, 3.1), ("am", 3.12, 3.3),
                    ("still", 3.32, 3.6), ("trying.", 3.62, 3.9)]
            r = sync.align(tts, al, perf, 4.0, Path(tmp) / "out.wav")
            spans = speech_spans(Path(tmp) / "out.wav")
            self.assertEqual(len(r["phrases"]), 2)
            self.assertAlmostEqual(spans[0][0], 2.2, delta=0.1)
            self.assertAlmostEqual(spans[-1][0], 3.0, delta=0.1)
            bad = sync.align(tts, al, perf[:-1], 4.0, Path(tmp) / "bad.wav")
            self.assertFalse(bad["ok"])                  # performance did not say the line -> redo motion


class UploadGate(unittest.TestCase):
    def test_privacy_is_private_or_unlisted_only(self):
        from studio import youtube_upload as yt
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            video.write_bytes(b"\0" * 10)
            cfg = {"category_id": "24", "contains_synthetic_media": True}
            self.assertEqual(yt.make_plan(video, "S", 1, "T", "d", [], config=cfg).privacy, "unlisted")
            self.assertEqual(yt.make_plan(video, "S", 1, "T", "d", [], config={**cfg, "privacy": "private"})
                             .body()["status"]["privacyStatus"], "private")
            with self.assertRaises(yt.UploadError):
                yt.make_plan(video, "S", 1, "T", "d", [], config={**cfg, "privacy": "public"})

    def test_queue_refuses_unapproved_director_episode(self):
        import youtube_queue as q
        from studio import youtube_upload as yt
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ep = root / "shows" / "s" / "episodes" / "ep01"
            ep.mkdir(parents=True)
            (ep / "shotlist.json").write_text("{}")
            video = root / "v.mp4"
            video.write_bytes(b"x")
            with self.assertRaises(yt.UploadError):
                q.episode_metadata("s", 1, video, {}, uploads_dir=root / "uploads", series_dir=root / "series")


if __name__ == "__main__":
    unittest.main()
