"""Offline tests for proposal, cast locks, integrity, and human approval stages."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import Scene, Story, approve_script, script_hash, produce, plan
from studio.casting import propose_cast
from studio.series import (SeriesBible, approve_cast, create_cast_template,
                           create_series, get_series, read_json, write_json)
from studio.story_checks import preflight

TEMPLATE_ROOT = Path(__file__).resolve().parents[1]


class CastingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "examples").mkdir()
        (self.root / "examples" / "series_bible.template.json").write_bytes(
            (TEMPLATE_ROOT / "examples" / "series_bible.template.json").read_bytes()
        )
        create_series(self.root, "the-last-signal")

    def test_unapproved_cast_blocked_by_preflight(self):
        bible, ledger = get_series(self.root, "the-last-signal")
        episode = self.fake_story()
        self.assertFalse(preflight(episode, bible, ledger)["passed"])

    def test_offline_proposal_then_approval_locks_bible(self):
        path = create_cast_template(self.root, "the-last-signal")
        self.assertTrue(path.exists())
        approve_cast(self.root, "the-last-signal")
        bible, _ = get_series(self.root, "the-last-signal")
        self.assertTrue(bible.cast_approved)
        self.assertEqual(bible.cast_version, 1)
        self.assertTrue((path.parent / "cast_lock.json").is_file())
        with self.assertRaises(ValueError):
            approve_cast(self.root, "the-last-signal")

    def test_cast_mutation_is_detected(self):
        create_cast_template(self.root, "the-last-signal")
        approve_cast(self.root, "the-last-signal")
        path = self.root / "series" / "the-last-signal" / "series_bible.json"
        raw = read_json(path)
        raw["characters"][0]["name"] = "A different person"
        write_json(path, raw)
        with self.assertRaisesRegex(ValueError, "changed without recasting approval"):
            get_series(self.root, "the-last-signal")

    def test_invalid_cast_voice_mapping_rejected(self):
        data = read_json(self.root / "series" / "the-last-signal" / "series_bible.json")
        del data["voices"]["mira"]
        with self.assertRaisesRegex(ValueError, "No voice profile"):
            SeriesBible.model_validate(data)

    def test_story_plan_requires_explicit_budget_approval(self):
        with self.assertRaisesRegex(ValueError, "confirm-spend"):
            plan("a mysterious radio", series_slug="the-last-signal", confirm_spend=False)

    def test_cast_proposal_requires_explicit_budget_approval(self):
        with self.assertRaisesRegex(ValueError, "confirm-spend"):
            propose_cast(self.root, "the-last-signal", "mystery at midnight", False)

    def test_produce_requires_approved_script_before_optional_api_imports(self):
        episode_dir = self.root / "outputs" / "episode"
        episode_dir.mkdir(parents=True)
        (episode_dir / "story.json").write_text(self.fake_story().model_dump_json(), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Script not approved"):
            produce(episode_dir, confirm_spend=True)

    def test_script_review_requires_confirmation(self):
        episode_dir = self.root / "outputs" / "episode"
        episode_dir.mkdir(parents=True)
        # A non-series script can be reviewed offline without a cast or network calls.
        story = self.fake_story()
        story.series_slug = None
        story.episode_number = None
        story.character_ids = []
        for scene in story.scenes:
            scene.character_ids = []
        (episode_dir / "story.json").write_text(story.model_dump_json(), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "confirm-reviewed"):
            approve_script(episode_dir, False)
        approve_script(episode_dir, True)
        status = read_json(episode_dir / "status.json")
        self.assertTrue(status["script_approved"])
        self.assertEqual(status["approved_story_sha256"], script_hash(episode_dir))
        # After edits, saved approval no longer matches the script hash.
        new_story = read_json(episode_dir / "story.json")
        new_story["title"] = "A Different Episode Title"
        write_json(episode_dir / "story.json", new_story)
        self.assertNotEqual(status["approved_story_sha256"], script_hash(episode_dir))

    @staticmethod
    def fake_story():
        return Story(
            title="The Silent Frequency",
            hook="The voice had never existed",
            summary="A kind radio engineer must choose between a forgotten memory and her family.",
            scenes=[Scene(narration="Mira recognizes a familiar voice through a radio with no batteries.",
                          visual_prompt="Mira in indigo coat stands under a window with cold rain.",
                          character_ids=["mira"]) for _ in range(3)],
            youtube_description="An original audio mystery featuring a radio engineer and her brother.",
            tags=["sci-fi", "mystery"], series_slug="the-last-signal", episode_number=1,
            character_ids=["mira"], emotional_turn="Fear gives way to care",
            ending_payoff="Mira chooses her brother", canon_updates=["Mira hears the signal"],
        )


if __name__ == "__main__":
    unittest.main()
