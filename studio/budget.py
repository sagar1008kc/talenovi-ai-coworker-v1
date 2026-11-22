"""Hard spending caps: per video, per run and per calendar month.

Every paid call must `reserve()` its worst-case estimate BEFORE it is sent. If any cap would
be exceeded the call is refused. After the call, `settle()` records the actual (or estimated)
cost. Costs are estimates from published price tables in config/providers.json; reconcile
with each provider's billing dashboard. This is a local guard, not a provider-side limit.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid

from pydantic import BaseModel, Field

from studio.models import utc_now


class BudgetExceeded(RuntimeError):
    """Raised before a paid call that would exceed a cap. Scope is video, run or month."""

    def __init__(self, scope: str, message: str):
        super().__init__(message)
        self.scope = scope


class BudgetCaps(BaseModel):
    max_usd_per_video: float = Field(gt=0)
    max_usd_per_run: float = Field(gt=0)
    max_usd_per_month: float = Field(gt=0)


class CostEntry(BaseModel):
    entry_id: str
    provider: str
    model: str
    item: str
    estimated_usd: float
    actual_usd: float | None = None
    status: str = "reserved"  # reserved | ok | failed_unbilled | possibly_billed
    created_utc: str = Field(default_factory=utc_now)
    detail: dict = Field(default_factory=dict)

    @property
    def counted_usd(self) -> float:
        """Conservative amount counted against caps."""
        if self.status == "failed_unbilled":
            return 0.0
        return self.actual_usd if self.actual_usd is not None else self.estimated_usd


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


class MonthlySpend:
    """Persistent month-to-date total shared by every run (state/spend_YYYY-MM.json)."""

    def __init__(self, state_dir: Path, now: datetime | None = None):
        month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
        self.path = state_dir / f"spend_{month}.json"
        self._lock = threading.Lock()

    def total(self) -> float:
        if not self.path.exists():
            return 0.0
        return float(json.loads(self.path.read_text(encoding="utf-8")).get("total_usd", 0.0))

    def add(self, usd: float) -> None:
        with self._lock:
            total = self.total() + usd
            _atomic_write(self.path, {"total_usd": round(total, 6), "updated_utc": utc_now()})


class RunSpend:
    """In-memory total for a single process run."""

    def __init__(self) -> None:
        self.total = 0.0


class CostLedger:
    """Per-video ledger persisted as cost_report.json in the video folder."""

    def __init__(self, path: Path, caps: BudgetCaps, run: RunSpend, month: MonthlySpend):
        self.path, self.caps, self.run, self.month = path, caps, run, month
        self.entries: list[CostEntry] = []
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.entries = [CostEntry.model_validate(e) for e in data.get("entries", [])]

    # ------------------------------------------------------------------ queries
    def video_total(self) -> float:
        return sum(e.counted_usd for e in self.entries)

    def by_provider(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for e in self.entries:
            totals[e.provider] = round(totals.get(e.provider, 0.0) + e.counted_usd, 6)
        return totals

    # ------------------------------------------------------------------ mutations
    def reserve(self, provider: str, model: str, item: str, estimated_usd: float, **detail) -> str:
        if estimated_usd < 0:
            raise ValueError("Cost estimate cannot be negative")
        checks = [
            ("video", self.video_total(), self.caps.max_usd_per_video),
            ("run", self.run.total, self.caps.max_usd_per_run),
            ("month", self.month.total(), self.caps.max_usd_per_month),
        ]
        for scope, spent, cap in checks:
            if spent + estimated_usd > cap + 1e-9:
                raise BudgetExceeded(scope, (
                    f"{item}: estimated ${estimated_usd:.3f} would exceed the {scope} cap "
                    f"(${spent:.2f} spent of ${cap:.2f})"))
        entry = CostEntry(entry_id=uuid.uuid4().hex[:12], provider=provider, model=model,
                          item=item, estimated_usd=round(estimated_usd, 6), detail=detail)
        self.entries.append(entry)
        # Reservations count immediately in every scope so a crash never under-reports.
        self.run.total += entry.estimated_usd
        self.month.add(entry.estimated_usd)
        self._save()
        return entry.entry_id

    def settle(self, entry_id: str, status: str, actual_usd: float | None = None, **detail) -> None:
        entry = next(e for e in self.entries if e.entry_id == entry_id)
        before = entry.counted_usd
        entry.status = status
        if actual_usd is not None:
            entry.actual_usd = round(actual_usd, 6)
        entry.detail.update(detail)
        delta = entry.counted_usd - before
        self.run.total += delta
        self.month.add(delta)
        self._save()

    def _save(self) -> None:
        _atomic_write(self.path, self.report())

    def report(self) -> dict:
        return {
            "currency": "USD",
            "video_total_usd": round(self.video_total(), 4),
            "by_provider_usd": self.by_provider(),
            "caps": self.caps.model_dump(),
            "method": ("Estimates from published per-unit prices in config/providers.json and "
                       "token usage returned by the Claude API. Reconcile with provider dashboards."),
            "entries": [e.model_dump() for e in self.entries],
        }
