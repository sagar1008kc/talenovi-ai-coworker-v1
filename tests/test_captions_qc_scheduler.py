"""Captions, shot chunking, content QC checks and scheduler definitions (offline)."""
from pathlib import Path
import plistlib
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from studio import qc, scheduler
from studio.autopilot import ScheduleConfig, split_into_chunks, similarity
from studio.captions import build_cues, parse_srt, words_from_alignment, write_ass, write_srt
from studio.models import SHORT_FORM, Asset, Claim, ResearchBrief, Shot, Storyboard
from studio.render import assign_shot_timing


def alignment_for(text: str, seconds: float) -> dict:
    n = len(text)
    return {"characters": list(text),
            "character_start_times_seconds": [i * seconds / n for i in range(n)],
            "character_end_times_seconds": [(i + 1) * seconds / n for i in range(n)]}


class CaptionTests(unittest.TestCase):
    def test_words_and_cues_from_alignment(self):
        text = "Tides turn mills. Water waits for no one at all today."
        words = words_from_alignment(alignment_for(text, 5.0), offset=10.0)
        self.assertEqual([w.text for w in words][:3], ["Tides", "turn", "mills."])
        self.assertGreaterEqual(words[0].start, 10.0)
        cues = build_cues(words, max_words=3)
        self.assertEqual(cues[0].text, "Tides turn mills.")  # sentence boundary ends a cue
        self.assertTrue(all(len(c.text.split()) <= 3 for c in cues))
        self.assertTrue(all(b.start >= a.end - 1e-9 for a, b in zip(cues, cues[1:])))

    def test_srt_round_trip_and_ass_style(self):
        with TemporaryDirectory() as tmp:
            words = words_from_alignment(alignment_for("one two three four five", 2.0))
            cues = build_cues(words, 2)
            srt = write_srt(cues, Path(tmp) / "c.srt")
            self.assertEqual([c.text for c in parse_srt(srt)], [c.text for c in cues])
            ass = write_ass(cues, Path(tmp) / "c.ass", SHORT_FORM).read_text()
            self.assertIn("PlayResX: 1080", ass)
            self.assertIn("ONE TWO", ass)  # vertical captions are upper-cased for legibility

    def test_chunks_preserve_text_exactly(self):
        text = ("The wheel turned twice a day, every day, for six hundred years. " * 6).strip()
        chunks = split_into_chunks(text, 14, 24)
        self.assertEqual(" ".join(chunks), " ".join(text.split()))
        self.assertTrue(all(len(c.split()) <= 24 for c in chunks))

    def test_shot_timing_follows_spoken_words(self):
        shots = [Shot(shot_id="L00-00", section_index=0, narration="Alpha beta gamma.", visual_prompt="v"),
                 Shot(shot_id="L00-01", section_index=0, narration="Delta epsilon.", visual_prompt="v"),
                 Shot(shot_id="L01-00", section_index=1, narration="Zeta eta theta.", visual_prompt="v")]
        board = Storyboard(profile="long_16x9", title="t", style="s", section_headings=["a", "b"], shots=shots)
        a0 = alignment_for("Alpha beta gamma. Delta epsilon.", 4.0)
        a1 = alignment_for("Zeta eta theta.", 2.0)
        timed = assign_shot_timing(board, [a0, a1], [0.0, 4.6], [4.6, 2.8])
        self.assertAlmostEqual(timed.shots[1].start_seconds, 18 * 4.0 / 32, places=2)
        self.assertAlmostEqual(timed.shots[2].start_seconds, 4.6)
        self.assertAlmostEqual(sum(s.duration_seconds for s in timed.shots), 7.4, places=2)

    def test_topic_similarity(self):
        self.assertGreater(similarity("How tide mills worked", "How tide mills worked in Europe"), 0.62)
        self.assertLess(similarity("How tide mills worked", "Why glaciers sing before they calve"), 0.62)


class ContentQCTests(unittest.TestCase):
    def test_factual_checks_flag_unsourced_and_unknown_claims(self):
        brief = ResearchBrief(summary="s", open_questions=[], claims=[
            Claim(claim_id="c1", statement="a", source_urls=["https://ok.org"], confidence="high"),
            Claim(claim_id="c2", statement="b", source_urls=["https://invented.org"], confidence="low")])
        checks = {c.name: c for c in qc.factual_checks(brief, {"https://ok.org"}, {"c1", "c2", "c9"}, "pass")}
        self.assertEqual(checks["claims_exist"].status, "fail")
        self.assertEqual(checks["claims_sourced"].status, "fail")
        self.assertEqual(checks["low_confidence_claims"].status, "warn")
        ok = {c.name: c for c in qc.factual_checks(brief, {"https://ok.org"}, {"c1"}, "pass")}
        self.assertTrue(all(c.status == "pass" for c in ok.values()))

    def test_provenance_requires_records_and_flags_unconfirmed_licenses(self):
        assets = [Asset(asset_id="img", kind="image", path="a.png", provider="fal", model="m", license="terms"),
                  Asset(asset_id="bad", kind="image", path="b.png", provider="fal", model="", license="")]
        checks = {c.name: c for c in qc.provenance_checks(assets, False, music_required=True)}
        self.assertEqual(checks["asset_provenance"].status, "fail")
        self.assertEqual(checks["commercial_license"].status, "warn")
        self.assertEqual(checks["voice_license"].status, "warn")
        self.assertEqual(checks["music"].status, "warn")

    def test_caption_coverage_and_repeated_visuals(self):
        with TemporaryDirectory() as tmp:
            words = words_from_alignment(alignment_for("a b c d e f g h", 4.0))
            srt = write_srt(build_cues(words, 3), Path(tmp) / "c.srt")
            self.assertEqual(qc.caption_checks(srt, 8, 4.5)[0].status, "pass")
            self.assertEqual(qc.caption_checks(srt, 20, 4.5)[0].status, "fail")
            self.assertEqual(qc.caption_checks(None, 8, 4.5)[0].status, "fail")
            self.assertEqual(qc.caption_checks(srt, 8, 9.0)[0].status, "fail")  # long silent tail
            self.assertEqual(qc.caption_checks(srt, 8, 9.0, final_hold=2.5)[0].status, "pass")
            paths = []
            for i, color in enumerate([(0, 0, 0)] * 4 + [(255, 255, 255)]):
                img = Image.new("RGB", (32, 32), color)
                img.paste((200, 0, 0), (0, 0, 16, 32))
                paths.append(Path(tmp) / f"{i}.png")
                img.save(paths[-1])
            self.assertEqual(qc.repeated_visual_checks(paths)[0].status, "fail")

    def test_qc_report_fails_when_any_check_fails(self):
        report = qc.QCReport(profile="p", video="v.mp4", checks=[qc.Check(name="a", status="pass"),
                                                                 qc.Check(name="b", status="fail")])
        self.assertFalse(report.passed)
        self.assertTrue(report.summary()["human_review_required"])


class SchedulerTests(unittest.TestCase):
    def test_launchd_plist_runs_daily_with_consent_flag(self):
        plist = plistlib.loads(scheduler.launchd_plist(Path("/proj"), ScheduleConfig(hour=4, minute=5), "/py"))
        self.assertEqual(plist["ProgramArguments"], ["/py", "/proj/main.py", "auto-run", "--confirm-spend"])
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 4, "Minute": 5})
        self.assertEqual(plist["WorkingDirectory"], "/proj")
        self.assertFalse(plist["RunAtLoad"])

    def test_multi_day_interval_and_other_os_definitions(self):
        sched = ScheduleConfig(hour=2, minute=0, every_n_days=3)
        self.assertEqual(plistlib.loads(scheduler.launchd_plist(Path("/p"), sched))["StartInterval"], 3 * 86400)
        units = scheduler.systemd_units(Path("/p"), sched, "/py")
        self.assertIn("OnCalendar=*-*-1/3 02:00:00", units["talenovi-autopilot.timer"])
        self.assertIn("auto-run", units["talenovi-autopilot.service"])
        self.assertIn("/MO", scheduler.windows_command(Path("/p"), sched, "py"))

    def test_install_requires_explicit_unattended_consent(self):
        with self.assertRaises(ValueError):
            scheduler.install(Path("/p"), ScheduleConfig(), confirm_unattended_spend=False)


if __name__ == "__main__":
    unittest.main()
