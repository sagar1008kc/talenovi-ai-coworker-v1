"""Offline rehearsal of the v6 one-folder-per-story workflow (no network, no paid calls).

Everything runs in a disposable temporary project root: two different stories are started, one is
rendered with fake providers, approved, released and cleaned, then resumed; the other is stopped by the
duration gate before any image is made. Cleanup safety (symlinks, escaping paths, protected files,
repeat runs, other stories, owner files) is exercised on the same disposable data.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import tempfile
import unittest

from PIL import Image, ImageDraw

import main
from studio.episode_production import DurationOutOfRange, Production, dry_run_folder
from studio.series import approve_cast, get_series, series_path, story_contract_errors, SeriesBible, write_json
from studio.stories import (ScratchManifest, UnsafePath, cleanup_episode, continue_story, episode_dirs,
                            safe_relative, start_story, story_status)

REPO = Path(__file__).resolve().parents[1]
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
VOCAB = ("lantern harbor ledger quiet morning kettle window ferry rope gull letter drawer stairs bakery "
         "bicycle umbrella orchard market ticket station radio blanket compass garden porch candle notebook "
         "bridge tide pebble sweater mitten chalk violin attic railway puddle meadow cobble shutter heron "
         "copper thimble satchel postcard hammock pantry lamp kite button saucer basket ribbon").split()


def text(rng: random.Random, words: int) -> str:
    body = " ".join(rng.choice(VOCAB) for _ in range(words))
    return body[0].upper() + body[1:] + "."


def make_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="story-rehearsal-"))
    shutil.copytree(REPO / "config", root / "config")
    shutil.copytree(REPO / "examples", root / "examples")
    (root / "state").mkdir()
    return root


def cast(slug: str, title: str, planned: int, names: tuple[str, str]) -> dict:
    a, b = names
    ida, idb = a.split()[0].lower(), b.split()[0].lower()
    person = lambda name, other, age, look: {
        "id": name.split()[0].lower(), "name": name, "age_range": age, "appearance": look,
        "signature_wardrobe": "rust-orange raincoat, grey scarf", "temperament": "dry, observant",
        "core_desire": "to keep the family shop open", "fear_or_wound": "being the one who gave up",
        "speech_style": "short sentences, answers questions with questions",
        "relationships": {other: "sibling"}, "signature_prop": "brass key on a red cord",
        "deflection_example": "\"Is that what you came all this way to ask?\""}
    return {
        "slug": slug, "title": title, "premise": "Two siblings inherit a shop that only opens when it rains.",
        "genre": ["family drama"], "tone": "warm, wry", "target_audience": "general 13+",
        "visual_style": "soft overcast realism, teal and rust palette", "world_rules": ["The shop opens in rain."],
        "season_goal": "Decide together whether to keep the shop.",
        "story_principles": ["Every episode changes the relationship."],
        "characters": [person(a, idb, "34", "tall woman, cropped black hair"),
                       person(b, ida, "29", "lean man, curly auburn hair")],
        "voices": {"narrator": {"env_key": "ELEVENLABS_NARRATOR_VOICE_ID"},
                   ida: {"env_key": "ELEVENLABS_SERIOUS_WOMAN_VOICE_ID"},
                   idb: {"env_key": "ELEVENLABS_COMEDIAN_MAN_VOICE_ID"}},
        "season_outline": [{"number": n, "premise": f"part {n}", "emotional_progression": "doubt to trust",
                            "distinct_payoff": f"payoff {n}", "forward_question": f"question after {n}?",
                            "lead_want": "keep the shop", "lead_choice": f"choice {n}",
                            "immediate_payoff": f"result {n}", "consequence_for_next": f"carry {n}"}
                           for n in range(1, planned + 1)],
        "identity": {"planned_episodes": planned, "setting": "a harbor town", "visual_palette": "teal, rust",
                     "camera_style": "handheld, close", "pacing": "patient", "sound": "rain, room tone",
                     "humor_or_emotional_tone": "wry warmth", "finale_question": "Do they keep the shop?"},
    }


def write_episode(folder: Path, slug: str, number: int, ids: tuple[str, str], seed: int, words: int) -> None:
    rng = random.Random(seed)
    speakers = ["narrator", ids[0], ids[1]]
    scenes = []
    for i in range(10):
        speaker = speakers[i % 3]
        scenes.append({"narration": text(rng, words), "speaker_id": speaker,
                       "visual_prompt": f"Harbor-town shop interior, scene {i + 1}, overcast light, no text.",
                       "character_ids": [ids[0]] if speaker == "narrator" else [speaker],
                       "emotional_beat": "uneasy", "visual_continuity_notes": "rust raincoat"})
    story = {"title": f"Rehearsal {slug} part {number}", "hook": "The shop door opens by itself.",
             "summary": "An offline rehearsal story used only to exercise the pipeline.", "scenes": scenes,
             "youtube_description": "Offline rehearsal only. Fiction. AI-generated visuals and voices.",
             "tags": ["rehearsal"], "series_slug": slug, "episode_number": number, "character_ids": list(ids),
             "emotional_turn": "she laughs at the leak", "ending_payoff": "they open together",
             "canon_updates": [f"part {number} happened"], "unresolved_threads": [f"who left the key ({number})?"],
             "character_knowledge": {ids[0]: ["the shop opens in rain"], ids[1]: ["the key is missing"]},
             "next_episode_starts": f"at dawn after part {number}, the key is gone"}
    shots = []
    for i, scene in enumerate(scenes):
        for letter, size in (("A", "wide"), ("B", "close")):
            shots.append({"shot_id": f"S{i + 1:02}-{letter}", "scene_id": f"S{i + 1:02}", "shot_size": size,
                          "visual_description": f"{size} shot, scene {i + 1}", "character_ids": scene["character_ids"],
                          "speaker_id": scene["speaker_id"], "listener_ids": [], "action": "turns the brass key",
                          "start_state": "door shut", "end_state": "door ajar",
                          "audio_intent": "voiceover" if scene["speaker_id"] == "narrator" else "off_camera",
                          "camera_movement": "push_in", "transition_in": "dissolve",
                          "motion_required": (i, letter) in ((1, "B"), (6, "A")),
                          "video_prompt": "hand turns the key, door swings" if (i, letter) in ((1, "B"), (6, "A"))
                          else "", "caption_zone": "high" if i == 3 else "mid"})
    write_json(folder / "story.json", story)
    write_json(folder / "storyboard.json", {"shots": shots, "music": {"source": "none"}})


class OfflineProviders(Production):
    """Production with fake providers: the real gates, timeline, render, QC and reports, but no network."""

    def _fake(self, fn, *args):
        self.dry_run = True
        try:
            return fn(*args)
        finally:
            self.dry_run = False

    def check_gates(self, confirm_spend):
        return self._fake(super().check_gates, confirm_spend)

    def voice_balance(self):
        return None

    def make_voice(self, *args):
        return self._fake(super().make_voice, *args)

    def make_clips(self, *args):
        return self._fake(super().make_clips, *args)

    def make_images(self, story, bible, shots, ledger, store):
        paths = []
        for i, shot in enumerate(shots):  # visually distinct placeholders so repeated-visual QC is meaningful
            dest = self.p("visuals", "images", f"{shot['shot_id']}.png")
            rng = random.Random(i * 7919)
            img = Image.new("RGB", (544, 960), tuple(rng.randrange(256) for _ in range(3)))
            draw = ImageDraw.Draw(img)
            for _ in range(14):
                x, y = rng.randrange(544), rng.randrange(960)
                draw.rectangle((x, y, x + rng.randrange(40, 260), y + rng.randrange(40, 400)),
                               fill=tuple(rng.randrange(256) for _ in range(3)))
            img.save(dest)
            paths.append(dest)
        return paths


def produce_offline(root: Path, folder: Path) -> Path:
    job = OfflineProviders(root, folder, max_fal_usd=6.0, max_voice_credits=10_000, motion_seconds=12,
                           width=360, height=640, music_default="none", log=lambda _m: None)
    return job.run(confirm_spend=True)


def snapshot(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


class StoryCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_start_never_overwrites_and_requires_five_or_six(self):
        first = start_story(self.root, "The Rain Shop", 5)
        again = start_story(self.root, "The Rain Shop", 6)
        self.assertEqual((first["slug"], again["slug"]), ("the-rain-shop", "the-rain-shop-2"))
        self.assertIn("already exists", again["note"])
        with self.assertRaises(FileExistsError):
            start_story(self.root, "Anything", 5, slug="the-rain-shop")
        with self.assertRaises(ValueError):
            start_story(self.root, "Too Long", 7)
        self.assertTrue(Path(first["episode_dir"]).is_dir())
        self.assertTrue((series_path(self.root, "the-rain-shop") / "references" / "manifest.json").is_file())

    def test_contract_requires_identity_causal_plan_and_deflection(self):
        data = cast("x", "X", 5, ("Ines Varga", "Tomas Varga"))
        self.assertEqual(story_contract_errors(SeriesBible.model_validate(data)), [])
        data["characters"][0]["deflection_example"] = None
        data["season_outline"] = data["season_outline"][:4]
        errors = story_contract_errors(SeriesBible.model_validate(data))
        self.assertTrue(any("deflection_example" in e for e in errors))
        self.assertTrue(any("1..5" in e for e in errors))

    def test_legacy_outputs_layout_is_found_without_dryruns(self):
        write_json(self.root / "series" / "old-show" / "series_bible.json",
                   cast("old-show", "Old", 5, ("Ines Varga", "Tomas Varga")) | {"identity": None})
        write_json(self.root / "series" / "old-show" / "continuity.json", {})
        (self.root / "outputs" / "old-show_ep01_pilot").mkdir(parents=True)
        (self.root / "outputs" / "old-show_ep01_pilot__dryrun").mkdir()
        self.assertEqual(list(episode_dirs(self.root, "old-show").values()),
                         [self.root / "outputs" / "old-show_ep01_pilot"])

    def test_existing_repository_locks_still_verify(self):
        for slug in ("middle-c", "the-wren-signal"):
            if (REPO / "series" / slug / "cast_lock.json").is_file():
                get_series(REPO, slug)  # raises if the fingerprint changed


class ManifestSafetyTests(unittest.TestCase):
    def test_paths_must_stay_inside_and_avoid_symlinks_and_protected_files(self):
        run = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp())
        (run / "work").mkdir()
        os.symlink(outside, run / "work" / "link")
        for bad in ("../x", str(outside / "f"), "work/link/f", "master_9x16.mp4", "visuals/images/S01-A.png",
                    "reports/jobs.json", "visuals", "."):
            with self.assertRaises(UnsafePath, msg=bad):
                safe_relative(run, bad)
        self.assertEqual(safe_relative(run, "work/segments/seg_01.mp4").as_posix(), "work/segments/seg_01.mp4")
        shutil.rmtree(run)
        shutil.rmtree(outside)


class RetakeTests(unittest.TestCase):
    def test_retire_is_exact_bounded_and_keeps_history(self):
        from studio.providers import JobStore
        from studio.providers.base import JobRecord
        store = JobStore(Path(tempfile.mkdtemp()) / "jobs.json")
        for key, status in (("image:S03-B", "downloaded"), ("image:S03-B2", "downloaded"), ("clip:S03-B:wan", "failed")):
            store.put(JobRecord(key=key, provider="fal", model_id="m", request_id="r", status=status))
        self.assertEqual(store.retire("image:S03-B", exact=True), 1)
        self.assertIsNotNone(store.get("image:S03-B2"))          # a similar shot id is never touched
        self.assertTrue(store.retire_failed("clip:S03-B:", 1))
        store.put(JobRecord(key="clip:S03-B:wan", provider="fal", model_id="m", request_id="r", status="failed"))
        self.assertFalse(store.retire_failed("clip:S03-B:", 1))   # retry budget used up
        self.assertEqual(sorted(r.key for r in JobStore(store.path).all()), ["clip:S03-B:wan", "clip:S03-B:wan#retired1", "image:S03-B#retired1", "image:S03-B2"])

    def test_reject_take_moves_asset_to_listed_scratch(self):
        from studio.stories import reject_take
        run = Path(tempfile.mkdtemp())
        (run / "visuals" / "images").mkdir(parents=True)
        Image.new("RGB", (8, 8)).save(run / "visuals" / "images" / "S02-A.png")
        moved = reject_take(run, "S02-A")["moved"]
        self.assertFalse((run / "visuals" / "images" / "S02-A.png").exists())
        listed = {e["path"] for e in ScratchManifest(run).entries.values()}
        self.assertEqual(set(moved), listed)
        with self.assertRaises(FileNotFoundError):
            reject_take(run, "S02-A")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe required for the offline render rehearsal")
class TwoStoryRehearsal(unittest.TestCase):
    def test_two_stories_resume_duration_gate_and_cleanup(self):
        root = make_root()
        self.addCleanup(shutil.rmtree, root, True)
        report: dict = {}

        # --- story A: start, cast, episode 01 through render, approval, release and cleanup
        a = start_story(root, "The Lantern Ledger", 5)
        write_json(series_path(root, a["slug"]) / "cast_proposal.json",
                   cast(a["slug"], a["title"], 5, ("Ines Varga", "Tomas Varga")))
        approve_cast(root, a["slug"])
        ep1 = Path(a["episode_dir"])
        resumed = continue_story(root, a["slug"])  # nothing written yet: resumes 01, never allocates 02
        self.assertEqual((resumed["action"], resumed["number"]), ("resume", 1))
        write_episode(ep1, a["slug"], 1, ("ines", "tomas"), seed=1, words=24)
        checks = main.episode_checks(ep1, main.load_story(ep1), root)
        self.assertTrue(checks["passed"], checks["errors"])
        self.assertTrue(60 <= checks["runtime_estimate_seconds"] <= 90, checks["runtime_estimate_seconds"])
        main.approve_script(ep1, True, "rehearsal", root=root)
        rehearsal_dir = dry_run_folder(ep1)
        self.assertEqual(rehearsal_dir, ep1 / "work" / "dryrun")  # inside the run dir, listed as scratch
        master = produce_offline(root, ep1)
        quality = json.loads((ep1 / "reports" / "quality_report.json").read_text())
        failed = [c for c in quality["checks"] if c["status"] == "fail"]
        self.assertTrue(quality["passed"], failed)
        names = {c["name"] for c in quality["checks"]}
        self.assertIn("required_motion", names)
        self.assertFalse((ep1 / "final").exists())  # story folders keep exactly one master
        (ep1 / "reports" / "editorial_review.md").write_text("PASS (offline rehearsal)\n")

        # Disposable hazards: an owner file, a symlink to another story, an escaping manifest entry.
        b = start_story(root, "Salt and Static", 6)
        b_dir = series_path(root, b["slug"])
        (ep1 / "owner_notes.txt").write_text("owner file, never listed")
        os.symlink(b_dir, ep1 / "work" / "to_other_story")
        manifest_path = ep1 / "scratch_manifest.json"
        tampered = json.loads(manifest_path.read_text())
        tampered["entries"] += [{"path": "../../episodes", "kind": "dir"}, {"path": "work/to_other_story",
                                                                         "kind": "dir"},
                                {"path": "master_9x16.mp4", "kind": "file"}]
        manifest_path.write_text(json.dumps(tampered))
        other_before = snapshot(b_dir)

        with self.assertRaises(ValueError):
            cleanup_episode(root, ep1)  # not approved yet: nothing may be removed
        size_before = sum(p.stat().st_size for p in ep1.rglob("*") if p.is_file() and not p.is_symlink())
        main.approve(ep1, True, "rehearsal", root=root)
        release = json.loads((ep1 / "release.json").read_text())
        self.assertEqual(release["master"]["sha256"], hashlib.sha256(master.read_bytes()).hexdigest())
        self.assertEqual((release["master"]["width"], release["master"]["height"]), (360, 640))
        self.assertTrue(60 <= release["master"]["duration_seconds"] <= 90)
        self.assertFalse((ep1 / "work" / "segments").exists())
        self.assertFalse((ep1 / "work" / "dryrun").exists())
        self.assertFalse((ep1 / "audio" / "narration.wav").exists())
        for kept in ("master_9x16.mp4", "story.json", "storyboard.json", "release.json", "owner_notes.txt",
                     "reports/quality_report.json", "visuals/images/S01-A.png", "audio/audio_segments/scene_01.mp3"):
            self.assertTrue((ep1 / kept).exists(), kept)
        self.assertTrue((ep1 / "work" / "to_other_story").is_symlink())
        self.assertEqual(snapshot(b_dir), other_before)  # the other story is untouched
        skipped = {s["path"] for s in json.loads(manifest_path.read_text())["cleanups"][-1]["skipped"]}
        self.assertEqual(skipped, {"../../episodes", "work/to_other_story", "master_9x16.mp4"})
        again = cleanup_episode(root, ep1)  # repeat cleanup is harmless
        self.assertEqual((again["removed"], again["reclaimed_bytes"]), (0, 0))
        self.assertGreater(release["master"]["bytes"], 0)
        storage = json.loads((ep1 / "release.json").read_text())["storage"]
        self.assertGreater(storage["reclaimed_bytes"], 0)
        refs = json.loads((series_path(root, a["slug"]) / "references" / "manifest.json").read_text())
        self.assertIn("ines", refs["characters"])
        _, ledger = get_series(root, a["slug"])
        self.assertEqual(ledger.approved_episodes[0].next_episode_starts, "at dawn after part 1, the key is gone")
        report["A"] = {"runtime_s": release["master"]["duration_seconds"], "kept_bytes": storage["retained_bytes"],
                       "reclaimed_bytes": storage["reclaimed_bytes"], "size_before": size_before}

        # --- resume story A: allocate 02, interrupt after script approval, resume 02 (not 03)
        nxt = continue_story(root, a["slug"])
        self.assertEqual((nxt["action"], nxt["number"]), ("new", 2))
        ep2 = Path(nxt["folder"])
        write_episode(ep2, a["slug"], 2, ("ines", "tomas"), seed=2, words=24)
        main.approve_script(ep2, True, "rehearsal", root=root)
        again = continue_story(root, a["slug"])
        self.assertEqual((again["action"], again["number"], again["stage"]), ("resume", 2, "SCRIPT_APPROVED"))
        status = story_status(root, a["slug"])
        self.assertEqual((status["completed_episodes"], status["next_episode"]), ([1], 2))
        self.assertIn("who left the key (1)?", status["open_dramatic_question"])

        # --- story B: cast, a far-too-short episode is stopped by the measured duration gate before images
        write_json(b_dir / "cast_proposal.json", cast(b["slug"], b["title"], 6, ("Wren Halloway", "Otto Halloway")))
        approve_cast(root, b["slug"])
        epb = Path(b["episode_dir"])
        write_episode(epb, b["slug"], 1, ("wren", "otto"), seed=3, words=6)
        checks = main.episode_checks(epb, main.load_story(epb), root)
        self.assertTrue(any("Estimated runtime" in w for w in checks["warnings"]))
        main.approve_script(epb, True, "rehearsal", root=root)
        with self.assertRaises(DurationOutOfRange):
            produce_offline(root, epb)
        self.assertFalse((epb / "visuals" / "images").exists())  # no image spend after a failed gate
        b_status = story_status(root, b["slug"])
        self.assertEqual(b_status["unfinished_episode"]["stage"], "DURATION_OUT_OF_RANGE")
        self.assertEqual(b_status["planned_episodes"], 6)
        with self.assertRaises(ValueError):
            cleanup_episode(root, epb)  # unfinished jobs and their assets are preserved
        print(f"\nREHEARSAL: {json.dumps(report)}")


if __name__ == "__main__":
    unittest.main()
