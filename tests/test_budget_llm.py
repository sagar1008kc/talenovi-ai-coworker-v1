"""Budget caps and Claude call handling with a scripted (offline) Anthropic client."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeAnthropic, claude_response, make_ledger
from studio.budget import BudgetCaps, BudgetExceeded, CostLedger, MonthlySpend, RunSpend
from studio.llm import ClaudeStudio, LLMError, strict_schema
from studio.models import ResearchBrief, ShotPlans, VideoScript
from studio.providers import load_catalog

CATALOG = load_catalog(Path(__file__).resolve().parents[1])
BRIEF = {"summary": "s", "open_questions": [],
         "claims": [{"claim_id": "c1", "statement": "x", "source_urls": ["https://a.org/1"], "confidence": "high"}]}


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_each_scope_is_enforced(self):
        for kwargs, scope in (({"video": 1}, "video"), ({"run": 1}, "run"), ({"month": 1}, "month")):
            ledger = make_ledger(self.dir / scope, **kwargs)
            ledger.reserve("fal", "m", "first", 0.8)
            with self.assertRaises(BudgetExceeded) as ctx:
                ledger.reserve("fal", "m", "second", 0.3)
            self.assertEqual(ctx.exception.scope, scope)

    def test_settle_adjusts_totals_and_persists(self):
        ledger = make_ledger(self.dir)
        entry = ledger.reserve("anthropic", "claude-opus-5-5", "script", 2.0)
        ledger.settle(entry, "ok", actual_usd=0.4)
        self.assertAlmostEqual(ledger.video_total(), 0.4)
        self.assertAlmostEqual(ledger.run.total, 0.4)
        self.assertAlmostEqual(ledger.month.total(), 0.4)
        reloaded = CostLedger(self.dir / "cost_report.json", ledger.caps, RunSpend(), MonthlySpend(self.dir))
        self.assertAlmostEqual(reloaded.video_total(), 0.4)

    def test_month_cap_spans_runs(self):
        caps = BudgetCaps(max_usd_per_video=10, max_usd_per_run=10, max_usd_per_month=1.0)
        CostLedger(self.dir / "a.json", caps, RunSpend(), MonthlySpend(self.dir)).reserve("x", "m", "i", 0.9)
        with self.assertRaises(BudgetExceeded):
            CostLedger(self.dir / "b.json", caps, RunSpend(), MonthlySpend(self.dir)).reserve("x", "m", "i", 0.2)

    def test_failed_unbilled_releases_reservation(self):
        ledger = make_ledger(self.dir)
        entry = ledger.reserve("fal", "m", "clip", 1.0)
        ledger.settle(entry, "failed_unbilled")
        self.assertEqual(ledger.video_total(), 0.0)


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = make_ledger(Path(self.tmp.name))

    def studio(self, responses):
        client = FakeAnthropic(responses)
        return ClaudeStudio(client, "claude-opus-5-5", CATALOG["claude"], "high"), client

    def test_schema_keeps_field_names_and_is_strict(self):
        schema = strict_schema(VideoScript)
        self.assertIn("title", schema["properties"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["$defs"]["ScriptSection"]["additionalProperties"])

    def test_research_records_seen_urls_cost_and_request_shape(self):
        llm, client = self.studio([claude_response(BRIEF, urls=["https://a.org/1"], searches=3,
                                                   input_tokens=10_000, output_tokens=2_000)])
        brief, seen = llm.research(self.ledger, "T", "A", max_searches=5)
        self.assertIsInstance(brief, ResearchBrief)
        self.assertEqual(seen, {"https://a.org/1"})
        kw = client.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["tools"][0]["type"], "web_search_20260209")
        self.assertEqual(kw["tools"][0]["max_uses"], 5)
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kw["extra_body"], {"fallbacks": "default"})
        self.assertNotIn("thinking", kw)
        expected = 10_000 * 4 / 1e6 + 2_000 * 20 / 1e6 + 3 * 0.01
        self.assertAlmostEqual(self.ledger.video_total(), expected)

    def test_pause_turn_is_continued(self):
        llm, client = self.studio([claude_response(None, urls=["https://a.org/1"], stop="pause_turn"),
                                   claude_response(BRIEF)])
        brief, seen = llm.research(self.ledger, "T", "A", 2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1]["messages"][1]["role"], "assistant")
        self.assertIn("https://a.org/1", seen)

    def test_refusal_and_truncation_raise(self):
        for stop in ("refusal", "max_tokens"):
            llm, _ = self.studio([claude_response(BRIEF, stop=stop)])
            with self.assertRaises(LLMError):
                llm.research(self.ledger, "T", "A", 1)

    def test_shot_plan_ids_must_match(self):
        bad = {"shots": [{"shot_id": "X", "visual_prompt": "v", "wants_motion": False, "camera": "zoom_in",
                          "motion_prompt": ""}]}
        llm, _ = self.studio([claude_response(bad)])
        with self.assertRaises(LLMError):
            llm.plan_shots(self.ledger, "t", "style", "16:9", [("L00-00", "text")], 1)
        good = {"shots": [dict(bad["shots"][0], shot_id="L00-00")]}
        llm, _ = self.studio([claude_response(good)])
        self.assertIsInstance(llm.plan_shots(self.ledger, "t", "style", "16:9", [("L00-00", "text")], 1), ShotPlans)

    def test_budget_blocks_call_before_request(self):
        llm, client = self.studio([claude_response(BRIEF)])
        with self.assertRaises(BudgetExceeded):
            llm.research(make_ledger(Path(self.tmp.name) / "tiny", video=0.01), "T", "A", 8)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
