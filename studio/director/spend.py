"""Per-shot / per-episode spend: caps from config/studio.json, a reserve-before-call ledger, an alert file.

The director never sets its own caps. It uses the owner's `episode_caps` (fal USD, ElevenLabs credits)
and `monthly_caps` and refuses a paid call that would cross any of them. Every reservation is recorded
in <episode>/reports/cost_report.json (with the shot_id) and appended to <episode>/reports/spend_log.jsonl.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from studio.budget import BudgetCaps, CostLedger, MonthlySpend, RunSpend
from studio.models import utc_now


class SpendCapExceeded(RuntimeError):
    pass


@dataclass
class EpisodeSpend:
    root: Path
    episode_dir: Path
    max_fal_usd: float
    max_voice_credits: int
    monthly_cap_usd: float
    voice_usd_per_1k: float = 0.22

    @classmethod
    def from_config(cls, root: Path, episode_dir: Path, cfg: dict) -> "EpisodeSpend":
        caps = cfg["episode_caps"]
        return cls(root=root, episode_dir=episode_dir, max_fal_usd=float(caps["max_fal_usd"]),
                   max_voice_credits=int(caps["max_voice_credits"]),
                   monthly_cap_usd=float(cfg["monthly_caps"]["max_total_usd"]))

    def __post_init__(self) -> None:
        voice_usd = self.max_voice_credits / 1000 * self.voice_usd_per_1k
        cap = self.max_fal_usd + voice_usd
        caps = BudgetCaps(max_usd_per_video=cap, max_usd_per_run=cap, max_usd_per_month=self.monthly_cap_usd)
        reports = self.episode_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        self.ledger = CostLedger(reports / "cost_report.json", caps, RunSpend(), MonthlySpend(self.root / "state"))
        self.log_path = reports / "spend_log.jsonl"

    # ------------------------------------------------------------ queries
    def fal_spent(self) -> float:
        return self.ledger.by_provider().get("fal", 0.0)

    def credits_spent(self) -> int:
        return sum(int(e.detail.get("credits", e.detail.get("characters", 0))) for e in self.ledger.entries
                   if e.provider == "elevenlabs" and e.status != "failed_unbilled")

    def by_shot(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.ledger.entries:
            sid = e.detail.get("shot_id") or e.item.split()[0]
            out[sid] = round(out.get(sid, 0.0) + e.counted_usd, 6)
        return out

    def month_spent(self) -> float:
        return self.ledger.month.total()

    # ------------------------------------------------------------ guards
    def guard_fal(self, usd: float, item: str) -> None:
        spent = self.fal_spent()
        if spent + usd > self.max_fal_usd + 1e-9:
            raise SpendCapExceeded(f"{item}: ${usd:.3f} would exceed the per-episode fal cap "
                                   f"(${spent:.2f} of ${self.max_fal_usd:.2f} used)")
        if self.month_spent() + usd > self.monthly_cap_usd + 1e-9:
            raise SpendCapExceeded(f"{item}: ${usd:.3f} would exceed the monthly cap "
                                   f"(${self.month_spent():.2f} of ${self.monthly_cap_usd:.2f})")

    def guard_credits(self, credits: int, item: str) -> None:
        spent = self.credits_spent()
        if spent + credits > self.max_voice_credits:
            raise SpendCapExceeded(f"{item}: {credits} ElevenLabs credits would exceed the per-episode cap "
                                   f"({spent} of {self.max_voice_credits} used)")

    def note(self, shot_id: str, kind: str, usd: float, **detail) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"utc": utc_now(), "shot_id": shot_id, "kind": kind, "usd": round(usd, 4),
                                **detail}) + "\n")

    def summary(self) -> dict:
        return {"fal_usd": round(self.fal_spent(), 4), "fal_cap_usd": self.max_fal_usd,
                "elevenlabs_credits": self.credits_spent(), "credit_cap": self.max_voice_credits,
                "month_usd": round(self.month_spent(), 4), "month_cap_usd": self.monthly_cap_usd,
                "by_shot_usd": self.by_shot()}


def raise_alert(episode_dir: Path, kind: str, message: str, **detail) -> Path:
    """Stop-and-alert record: the run ends, the owner (or Claude) reads reports/ALERT.json."""
    path = episode_dir / "reports" / "ALERT.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"utc": utc_now(), "kind": kind, "message": message, **detail}, indent=2) + "\n",
                    encoding="utf-8")
    return path
