"""Offline tests for the paid series-episode producer: timing math, hard caps and approval gates."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from studio.budget import CostEntry
from studio.captions import Cue
from studio.episode_production import (Production, SpendCapExceeded, Timeline, caption_timeline,
                                       dry_run_folder, normalize_move, shot_windows, transition_for,
                                       xfade_plan, zoompan_expr)
from studio.voices import describe_roster, roster_keys


class ImagePromptTests(unittest.TestCase):
    def test_framing_first_and_headcount(self):
        from types import SimpleNamespace
        from studio.episode_production import image_prompt
        ch = SimpleNamespace(id="ana", name="Ana Ruiz", age_range="30 years old", appearance="tall woman",
                             signature_wardrobe="red coat")
        bible = SimpleNamespace(characters=[ch], visual_style="Realism")
        scene = SimpleNamespace(character_ids=["ana"], visual_prompt="v", visual_continuity_notes="Dusk.")
        insert = image_prompt(bible, scene, {"shot_size": "insert", "character_ids": ["ana"],
                                             "visual_description": "a hand turns a key"})
        self.assertTrue(insert.startswith("Extreme close-up insert"))
        self.assertNotIn("tall woman", insert)
        close = image_prompt(bible, scene, {"shot_size": "close", "character_ids": ["ana"],
                                            "visual_description": "Ana looks up"})
        self.assertTrue(close.startswith("Tight close-up"))
        self.assertIn("tall woman", close)
        self.assertIn("Exactly one person", close)


class TimelineTests(unittest.TestCase):
    def test_scene_windows(self):
        tl = Timeline([3.0, 4.0, 5.0], gap=0.5, tail=1.0)
        self.assertEqual(tl.windows, [3.5, 4.5, 6.5])
        self.assertEqual(tl.starts, [0.0, 3.5, 8.0])
        self.assertEqual(tl.speech_starts, [0.5, 4.0, 8.5])
        self.assertAlmostEqual(tl.total, 14.5)

    def test_scene_holds_add_reaction_silence(self):
        from studio.episode_production import MAX_HOLD_S, scene_holds
        holds = scene_holds({"scene_holds": {"S02": 1.2, "S03": 9.0}}, 3)
        self.assertEqual(holds, [0.0, 1.2, MAX_HOLD_S])
        tl = Timeline([3.0, 4.0, 5.0], gap=0.5, tail=1.0, holds=holds)
        self.assertEqual(tl.windows, [3.5, 5.7, 6.5 + MAX_HOLD_S])
        self.assertEqual(tl.speech_starts[2], 3.5 + 5.7 + 0.5)
        self.assertEqual(scene_holds({}, 2), [0.0, 0.0])
        with self.assertRaises(ValueError):
            scene_holds({"scene_holds": {"S05": 1.0}}, 3)

    def test_shots_split_scene_windows_by_weight(self):
        w = shot_windows([4.0, 6.0], [0, 0, 1], [1.0, 3.0, 1.0])
        self.assertEqual(w, [1.0, 3.0, 6.0])
        with self.assertRaises(ValueError):
            shot_windows([4.0, 6.0], [0, 0], [1.0, 1.0])  # scene 2 has no shot

    def test_mixed_transition_durations_keep_total_length(self):
        windows, durs = [3.0, 2.0, 4.0, 5.0], [0.0, 0.35, 0.9, 0.12]
        lengths, offsets = xfade_plan(windows, durs)
        self.assertEqual(offsets, [3.0, 5.0, 9.0])
        out = lengths[0]
        for k in range(1, 4):  # simulate the xfade chain
            self.assertAlmostEqual(out - durs[k], offsets[k - 1])
            out = out + lengths[k] - durs[k]
        self.assertAlmostEqual(out, sum(windows))
        with self.assertRaises(ValueError):
            xfade_plan([0.5, 3.0], [0.0, 0.9])

    def test_transition_defaults(self):
        self.assertEqual(transition_for({}, 3, new_scene=False)[0], "dissolve")
        self.assertEqual(transition_for({}, 3, new_scene=True)[0], "fade")
        self.assertEqual(transition_for({"transition_in": "fadeblack"}, 3, True)[0], "fadeblack")

    def test_caption_timeline_covers_total_without_overlap(self):
        cues = [Cue(0.5, 1.0, "A"), Cue(0.9, 2.0, "B"), Cue(3.0, 3.5, "C")]
        items = caption_timeline(cues, 5.0)
        self.assertAlmostEqual(sum(d for _, d in items), 5.0)
        self.assertEqual([i for i, _ in items], [None, 0, 1, None, 2, None])

    def test_camera_moves(self):
        self.assertEqual(normalize_move("slow pull back to reveal the dish", 0), "pull_out")
        self.assertEqual(normalize_move("tilt up the ladder", 3), "tilt_up")
        self.assertIn("zoompan=", zoompan_expr("push_in", 90, 1080, 1920))


class CapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        self.job = Production(self.tmp, self.tmp / "ep", max_fal_usd=1.0, max_voice_credits=100,
                              log=lambda _m: None)

    def test_fal_cap_refuses_before_call(self):
        ledger = self.job.ledger()
        ledger.entries.append(CostEntry(entry_id="a", provider="fal", model="m", item="x",
                                        estimated_usd=0.9, status="ok"))
        self.job.guard_fal(ledger, 0.1, "ok-item")
        with self.assertRaises(SpendCapExceeded):
            self.job.guard_fal(ledger, 0.2, "too-much")

    def test_voice_credit_cap_ignores_unbilled_failures(self):
        ledger = self.job.ledger()
        ledger.entries.append(CostEntry(entry_id="b", provider="elevenlabs", model="m", item="x",
                                        estimated_usd=0.0, status="failed_unbilled",
                                        detail={"characters": 90}))
        ledger.entries.append(CostEntry(entry_id="c", provider="elevenlabs", model="m", item="x",
                                        estimated_usd=0.0, status="ok", detail={"characters": 60}))
        self.assertEqual(self.job.credits_spent(ledger), 60)
        self.job.guard_voice(ledger, 40, "fits")
        with self.assertRaises(SpendCapExceeded):
            self.job.guard_voice(ledger, 41, "over")


class GateTests(unittest.TestCase):
    def test_requires_confirm_spend_and_approved_script(self):
        tmp = Path(tempfile.mkdtemp())
        folder = tmp / "ep"
        folder.mkdir()
        (folder / "story.json").write_text("{}", encoding="utf-8")
        (folder / "status.json").write_text(json.dumps({"script_approved": False}), encoding="utf-8")
        job = Production(tmp, folder, max_fal_usd=1, max_voice_credits=1, log=lambda _m: None)
        with self.assertRaises(ValueError):
            job.check_gates(confirm_spend=False)
        try:
            job.check_gates(confirm_spend=True)
        except RuntimeError as exc:  # ffmpeg missing on this machine is also a refusal
            self.assertIn("Install", str(exc))
        except ValueError as exc:
            self.assertIn("not approved", str(exc))
        else:
            self.fail("unapproved script must be refused")

    def test_dry_run_never_writes_into_the_real_episode(self):
        tmp = Path(tempfile.mkdtemp())
        folder = tmp / "ep"
        folder.mkdir()
        for name in ("story.json", "status.json", "storyboard.json"):
            (folder / name).write_text("{}", encoding="utf-8")
        rehearsal = dry_run_folder(folder)
        self.assertNotEqual(rehearsal, folder)
        self.assertTrue((rehearsal / "story.json").is_file())


class MusicTests(unittest.TestCase):
    def test_prompt_maps_scene_cues_to_timestamps(self):
        from studio.music import build_prompt, music_credits
        prompt = build_prompt({"style": "solo piano", "cues": [
            {"from_scene": 1, "to_scene": 1, "mood": "uneasy"}, {"from_scene": 2, "to_scene": 3, "mood": "hopeful"}]},
            [0.0, 30.0, 61.0], 95.0)
        self.assertIn("0:00-0:30: uneasy", prompt)
        self.assertIn("0:30-1:35: hopeful", prompt)
        self.assertEqual(music_credits(100), 1500)

    def test_unlicensed_library_track_is_refused(self):
        from studio.music import library_track
        tmp = Path(tempfile.mkdtemp())
        (tmp / "assets" / "music").mkdir(parents=True)
        (tmp / "assets" / "music" / "provenance.json").write_text(json.dumps(
            {"tracks": [{"file": "x.mp3", "commercial_use_confirmed": False}]}))
        with self.assertRaises(ValueError):
            library_track(tmp, "x.mp3")


class RosterTests(unittest.TestCase):
    def test_roles_from_env_names(self):
        env = {"ELEVENLABS_CHILD_GIRL_VOICE_ID": "a", "ELEVENLABS_SERIOUS_WOMAN_VOICE_ID": "b",
               "ELEVENLABS_VOICE_ID": "${ELEVENLABS_NARRATOR_VOICE_ID}", "ELEVENLABS_NARRATOR_VOICE_ID": "c",
               "ELEVENLABS_BOY_VOICE_ID": "", "ELEVENLABS_API_KEY": "secret"}
        self.assertEqual(list(roster_keys(env)), ["child_girl", "narrator", "serious_woman"])

    def test_describe_never_includes_ids(self):
        env = {"ELEVENLABS_CHILD_GIRL_VOICE_ID": "id1", "ELEVENLABS_CHILD_BOY_VOICE_ID": "id1"}
        rows = describe_roster(env, lambda vid: {"name": "Kid", "labels": {"gender": "female"}})
        self.assertNotIn("id1", str(rows))
        self.assertEqual(rows[1]["same_voice_as"], "child_boy")


if __name__ == "__main__":
    unittest.main()
