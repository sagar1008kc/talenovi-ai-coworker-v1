"""Orchestrator tests with offline fakes. Stages before narration need no FFmpeg; the full
end-to-end dry run is skipped automatically when ffmpeg/ffprobe are not installed."""
from pathlib import Path
import json
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.autopilot import Autopilot, AutopilotConfig, approve_script, status_table
from studio.budget import BudgetExceeded
from studio.models import LONG_FORM, SHORT_FORM, FactReview
from studio.providers import load_catalog
from studio.services import FakeLLM, build_dryrun_services

ROOT = Path(__file__).resolve().parents[1]
CATALOG = load_catalog(ROOT)
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def config(**overrides) -> AutopilotConfig:
    data = json.loads((ROOT / "config" / "autopilot.example.json").read_text())
    data.update(overrides)
    return AutopilotConfig.model_validate(data)


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.outputs, self.state = self.root / "outputs", self.root / "state"

    def pilot(self, cfg=None, services=None, profiles=(LONG_FORM, SHORT_FORM)) -> Autopilot:
        return Autopilot(self.root, cfg or config(), services or build_dryrun_services(), outputs=self.outputs,
                         state_dir=self.state, catalog=CATALOG, profiles=profiles)

    def only_video(self) -> Path:
        folders = [p for p in self.outputs.iterdir() if p.is_dir()]
        self.assertEqual(len(folders), 1)
        return folders[0]

    def test_human_gate_stops_before_paid_media(self):
        results = self.pilot().run()
        self.assertEqual(results[0]["status"], "AWAITING_SCRIPT_APPROVAL")
        folder = self.only_video()
        state = json.loads((folder / "state.json").read_text())
        self.assertEqual(state["completed_stages"], ["topic", "research", "script", "fact_review", "storyboard"])
        for name in ("topic.json", "research.json", "script.md", "storyboard.json", "fact_review.json"):
            self.assertTrue((folder / name).is_file(), name)
        self.assertFalse((folder / "assets" / "voice").exists(), "no narration before approval")
        board = json.loads((folder / "storyboard_long.json").read_text())
        script = json.loads((folder / "script.json").read_text())
        self.assertEqual(" ".join(s["narration"] for s in board["shots"]),
                         " ".join(s["narration"] for s in script["sections"]))
        motion = [s for s in board["shots"] if s["kind"] == "motion"]
        self.assertLessEqual(len(motion), config().video.max_motion_shots_long)

    def test_approval_is_bound_to_script_fingerprint(self):
        self.pilot().run()
        folder = self.only_video()
        with self.assertRaises(ValueError):
            approve_script(folder, confirm_reviewed=False)
        approve_script(folder, confirm_reviewed=True)
        (folder / "short_script.json").write_text((folder / "short_script.json").read_text().replace("Short", "Edit"))
        pilot = self.pilot()
        # Video is resumable (approved) but the edited script no longer matches -> gate closes again.
        self.assertEqual(pilot.resumable_videos(), [folder])
        pilot._process(__import__("studio.autopilot", fromlist=["Video"]).Video(folder, pilot))
        self.assertEqual(json.loads((folder / "state.json").read_text())["status"], "AWAITING_SCRIPT_APPROVAL")

    def test_topics_are_not_repeated_across_runs(self):
        self.pilot().run()
        self.pilot().run()
        titles = [json.loads(p.read_text())["chosen"]["title"] for p in self.outputs.glob("*/topic.json")]
        self.assertEqual(len(titles), 2)
        self.assertEqual(len(set(titles)), 2)
        self.assertEqual(len(json.loads((self.state / "topic_history.json").read_text())), 2)

    def test_budget_exhaustion_pauses_without_failing(self):
        class PricyLLM(FakeLLM):
            def research(self, ledger, *a, **k):
                ledger.reserve("anthropic", "claude-opus-5-5", "research", 999.0)
        result = self.pilot(services=build_dryrun_services().__class__(
            **{**build_dryrun_services().__dict__, "llm": PricyLLM()}),
            cfg=config(budget={"max_usd_per_video": 5000, "max_usd_per_run": 50, "max_usd_per_month": 5000})).run()
        self.assertEqual(result[0]["status"], "PAUSED_BUDGET")
        self.assertIn("run cap", result[0]["reason"])

    def test_failed_stage_is_retried_selectively_next_run(self):
        calls = {"n": 0}

        class FlakyLLM(FakeLLM):
            def write_script(self, *a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient provider error")
                return super().write_script(*a, **k)

        services = build_dryrun_services()
        services.llm = FlakyLLM()
        first = self.pilot(services=services).run()
        self.assertEqual(first[0]["status"], "IN_PROGRESS")
        folder = self.only_video()
        state = json.loads((folder / "state.json").read_text())
        self.assertIn("research", state["completed_stages"])
        self.assertEqual(state["errors"][0]["stage"], "script")
        second = self.pilot(services=services).run(max_new=0)
        self.assertEqual(second[0]["status"], "AWAITING_SCRIPT_APPROVAL")

    def test_failed_fact_review_requires_human(self):
        class Skeptic(FakeLLM):
            def review_facts(self, *a, **k):
                return FactReview(verdict="fail", issues=[], notes="unsupported central claim")
        services = build_dryrun_services()
        services.llm = Skeptic()
        result = self.pilot(services=services).run()
        self.assertEqual(result[0]["status"], "NEEDS_REVIEW")
        self.assertEqual(self.pilot(services=services).resumable_videos(), [])

    def test_status_table(self):
        self.pilot().run()
        table = status_table(self.outputs, self.state)
        self.assertEqual(table["videos"][0]["status"], "AWAITING_SCRIPT_APPROVAL")

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
    def test_end_to_end_dry_run_renders_and_validates_both_formats(self):
        long_p = LONG_FORM.scaled(6).model_copy(update={"min_seconds": 20, "max_seconds": 60})
        short_p = SHORT_FORM.scaled(6).model_copy(update={"min_seconds": 8, "max_seconds": 30})
        cfg = config(script_review="automated", long_form_words=[90, 110], short_words=[30, 40])
        results = self.pilot(cfg=cfg, profiles=(long_p, short_p)).run()
        folder = self.only_video()
        report = json.loads((folder / "qc_report.json").read_text())
        failures = {k: v["failures"] for k, v in report.items()}
        self.assertEqual(results[0]["status"], "COMPLETE_PENDING_HUMAN_REVIEW", failures)
        for suffix in ("full_16x9", "short_9x16"):
            self.assertEqual(len(list(folder.glob(f"*__{suffix}.mp4"))), 1)
        for name in ("thumbnail.png", "metadata.json", "cost_report.json", "provenance.json", "captions_long.srt"):
            self.assertTrue((folder / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
