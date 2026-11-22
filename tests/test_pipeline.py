"""Schema and output folder naming tests; no provider network required."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import Scene, Story, slugify


class PipelineTests(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slugify("The Phone Call From 2050!"), "the-phone-call-from-2050")

    def test_story_narration(self):
        scenes = [Scene(narration="One story scene with an original mysterious event.",
                        visual_prompt="A vertical cinematic night city under rain and blue haze.") for _ in range(3)]
        story = Story(title="A Complete Story", hook="An impossible midnight call",
                      summary="Three continuous scenes create an atmospheric short story.",
                      scenes=scenes, youtube_description="An original fictional story about a strange phone call.",
                      tags=["mystery"])
        self.assertEqual(story.narration.count("One story scene"), 3)


if __name__ == "__main__":
    unittest.main()
