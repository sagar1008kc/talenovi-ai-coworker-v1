"""Offline validation of canon, speaker identities, and uniqueness checks."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import Story, Scene
from studio.series import (create_series, create_cast_template, approve_cast, get_series,
                          record_approval, read_json, write_json)
from studio.story_checks import preflight

ROOT = Path(__file__).resolve().parents[1]


def build_episode(**changes) -> Story:
    data = dict(
        title="The Silent Radio Signal",
        hook="The radio sang a melody she had forgotten",
        summary="Mira hears an impossible radio melody and risks a treasured memory to contact her younger brother.",
        scenes=[
            Scene(narration="Mira found the old radio playing a song from her childhood without batteries.",
                  visual_prompt="Mira in indigo jacket under desk lamp holding a silver pendant.",
                  speaker_id="narrator", character_ids=["mira"]),
            Scene(narration="A voice offered to bring back the song if she gave up a memory.",
                  visual_prompt="Mira turns the radio knob and stares at a dim studio clock.",
                  speaker_id="narrator", character_ids=["mira"]),
            Scene(narration="She chose one forgotten afternoon and finally remembered her brother's kindness.",
                  visual_prompt="Mira sees the night sky through the window and grips her necklace.",
                  speaker_id="narrator", character_ids=["mira"]),
        ],
        youtube_description="An original family-safe science fiction short about a memory-changing radio.",
        tags=["mystery", "sci-fi"],
        series_slug="the-last-signal", episode_number=1, character_ids=["mira"],
        emotional_turn="Mira moves from isolation to remembering kindness",
        ending_payoff="She remembers why she should call her brother",
        canon_updates=["Mira discovers the impossible radio frequency"],
        unresolved_threads=["Who controls the signal?"],
    )
    data.update(changes)
    return Story(**data)


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "examples").mkdir()
        (self.root / "examples" / "series_bible.template.json").write_bytes(
            (ROOT / "examples" / "series_bible.template.json").read_bytes()
        )
        create_series(self.root, "the-last-signal")
        create_cast_template(self.root, "the-last-signal")
        approve_cast(self.root, "the-last-signal")
        self.bible, self.canon = get_series(self.root, "the-last-signal")

    def test_series_created_with_locked_cast(self):
        self.assertEqual(self.bible.slug, "the-last-signal")
        self.assertEqual(self.canon.last_approved_episode, 0)
        self.assertTrue(self.bible.cast_approved)
        self.assertIn("mira", {c.id for c in self.bible.characters})

    def test_valid_episode_passes(self):
        result = preflight(build_episode(), self.bible, self.canon)
        self.assertTrue(result["passed"], result["errors"])

    def test_unknown_cast_fails(self):
        story = build_episode(character_ids=["mira", "unregistered"])
        result = preflight(story, self.bible, self.canon)
        self.assertFalse(result["passed"])
        self.assertTrue(any("Unregistered" in error for error in result["errors"]))

    def test_unknown_voice_fails(self):
        story = build_episode()
        story.scenes[0].speaker_id = "unknown_voice"
        self.assertFalse(preflight(story, self.bible, self.canon)["passed"])

    def test_no_vulgar_language(self):
        story = build_episode(title="That shit was impossible")
        self.assertFalse(preflight(story, self.bible, self.canon)["passed"])

    def test_approved_canon_blocks_duplicate(self):
        first = build_episode()
        record_approval(self.root, first)
        bible, canon = get_series(self.root, "the-last-signal")
        self.assertEqual(canon.last_approved_episode, 1)
        second = build_episode(episode_number=2)
        result = preflight(second, bible, canon)
        self.assertFalse(result["passed"])
        self.assertTrue(any("Too similar" in error for error in result["errors"]))

    def test_approval_updates_canon_only_once_per_sequence(self):
        first = build_episode()
        record_approval(self.root, first)
        with self.assertRaises(ValueError):
            record_approval(self.root, first)
        canon = read_json(self.root / "series" / "the-last-signal" / "continuity.json")
        self.assertEqual(len(canon["approved_episodes"]), 1)


if __name__ == "__main__":
    unittest.main()
