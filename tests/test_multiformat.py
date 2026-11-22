"""Free tests validating multi-platform format declarations."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.multiformat import FULL_HD, PREVIEW, dimensions_of, export_all, quality_report


class ExportFormatsTest(unittest.TestCase):
    def test_production_resolutions(self):
        self.assertEqual([(f.width, f.height) for f in FULL_HD], [(1080, 1920)])

    def test_preview_resolutions(self):
        self.assertEqual([(f.width, f.height) for f in PREVIEW], [(360, 640)])

    def test_distinct_filenames(self):
        self.assertEqual([f.filename for f in FULL_HD], ["master_9x16.mp4"])

    def test_reject_invalid_scene_timings(self):
        with self.assertRaises((ValueError, FileNotFoundError, RuntimeError)):
            export_all(Path("__nonexistent__"), 2, Path("absent.mp3"), [4, 4], [5])

if __name__ == "__main__":
    unittest.main()
