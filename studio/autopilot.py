"""Autonomous local orchestrator.

Topic -> Research -> Script (+ standalone Short) -> Fact review -> Storyboards -> [script gate]
-> Narration -> Stills -> Selective motion clips -> Thumbnail -> Render x2 -> QC -> Finalize.

Every stage is idempotent and recorded in <video>/state.json, so an interrupted or failed run
resumes without repeating completed paid work. Nothing is ever uploaded or published.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from studio.budget import BudgetCaps, BudgetExceeded, CostLedger, MonthlySpend, RunSpend
from studio.captions import build_cues, words_from_alignment, write_ass, write_srt
from studio.media import MusicLibrary, VoiceSettings
from studio.models import (LONG_FORM, SHORT_FORM, Asset, FactReview, RenderProfile, ResearchBrief,
                           Shot, ShortScript, Storyboard, TopicCandidate, VideoScript, utc_now)
from studio.providers.base import JobStore, ProviderError, VideoRequest

log = logging.getLogger("studio.autopilot")

STAGES = ["topic", "research", "script", "fact_review", "storyboard", "script_gate", "voice",
          "images", "motion", "thumbnail", "render", "qc", "finalize"]
Status = Literal["IN_PROGRESS", "AWAITING_SCRIPT_APPROVAL", "PAUSED_BUDGET", "NEEDS_REVIEW",
                 "FAILED", "COMPLETE_PENDING_HUMAN_REVIEW"]
RESUMABLE = {"IN_PROGRESS", "PAUSED_BUDGET", "AWAITING_SCRIPT_APPROVAL"}


# =========================================================================== configuration
class VideoConfig(BaseModel):
    fallback_order: list[str] = ["wan3_i2v", "veo31_fast_i2v", "kling4_i2v", "seedance25_i2v",
                                 "flux3max_i2v", "runway_gen45_i2v"]
    resolution: str = "720p"
    max_motion_shots_long: int = Field(default=8, ge=0)
    max_motion_shots_short: int = Field(default=3, ge=0)
    min_motion_shot_seconds: float = 3.0
    timeout_minutes: float = 20


class ClaudeConfig(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    research_searches: int = Field(default=8, ge=1, le=20)


class MusicConfig(BaseModel):
    library_dir: str = "assets/music"
    volume_db: float = -20


class ScheduleConfig(BaseModel):
    hour: int = Field(default=3, ge=0, le=23)
    minute: int = Field(default=15, ge=0, le=59)
    every_n_days: int = Field(default=1, ge=1)


class AutopilotConfig(BaseModel):
    niche: str
    audience: str
    channel_brief: str = ""
    visual_style: str
    videos_per_run: int = Field(default=1, ge=1, le=10)
    script_review: Literal["human", "automated"] = "human"
    schedule: ScheduleConfig = ScheduleConfig()
    budget: BudgetCaps
    claude: ClaudeConfig = ClaudeConfig()
    image_model: str = "flux2_flash"
    video: VideoConfig = VideoConfig()
    voice: VoiceSettings = VoiceSettings()
    music: MusicConfig = MusicConfig()
    long_form_words: tuple[int, int] = (1250, 1450)
    short_words: tuple[int, int] = (90, 130)
    dedupe_similarity: float = Field(default=0.62, gt=0, lt=1)
    max_stage_attempts: int = Field(default=3, ge=1)
    ai_frame_review: bool = True

    @classmethod
    def load(cls, path: Path) -> "AutopilotConfig":
        if not path.is_file():
            raise FileNotFoundError(f"{path} not found. Copy config/autopilot.example.json to "
                                    "config/autopilot.json and edit it.")
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


# =========================================================================== logging
class JsonLinesFormatter(logging.Formatter):
    RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                "logger": record.name, "msg": record.getMessage()}
        data.update({k: v for k, v in record.__dict__.items() if k not in self.RESERVED})
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging(logs_dir: Path) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"autopilot-{datetime.now().strftime('%Y%m%d')}.jsonl"
    root = logging.getLogger("studio")
    root.setLevel(logging.INFO)
    if not any(getattr(h, "_autopilot", False) for h in root.handlers):
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(JsonLinesFormatter())
        fh._autopilot = True  # type: ignore[attr-defined]
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        console._autopilot = True  # type: ignore[attr-defined]
        root.addHandler(fh)
        root.addHandler(console)
    return path


# =========================================================================== services seam
@dataclass
class Services:
    """Everything that costs money or touches the network, swappable for offline fakes."""
    llm: Any
    images: Any          # .generate(key=, prompt=, size=, dest=, store=, ledger=, should_stop=) / .spec
    make_clip: Callable[..., tuple[str, str, str]]  # (req, dest, store, ledger, should_stop) -> (provider, model, request_id)
    tts: Any             # .synthesize(text=, dest_audio=, previous_text=, next_text=, ledger=)
    dry_run: bool = False
    licenses: dict[str, str] = field(default_factory=dict)  # provider -> license note


# =========================================================================== helpers
def slugify(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "video"


def word_count(text: str) -> int:
    return len(text.split())


def split_into_chunks(text: str, min_words: int, max_words: int) -> list[str]:
    """Sentence-aware chunks for shots. Concatenating chunks with single spaces == text."""
    words_total = text.split()
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(words_total))
    pieces: list[str] = []
    for sentence in sentences:
        words = sentence.split()
        while len(words) > max_words:  # split very long sentences at a comma if possible
            cut = next((i + 1 for i in range(max_words - 1, min_words - 1, -1) if words[i].endswith(",")),
                       max_words)
            pieces.append(" ".join(words[:cut]))
            words = words[cut:]
        if words:
            pieces.append(" ".join(words))
    chunks: list[str] = []
    for piece in pieces:
        if chunks and word_count(chunks[-1]) < min_words and word_count(chunks[-1]) + word_count(piece) <= max_words:
            chunks[-1] = f"{chunks[-1]} {piece}"
        else:
            chunks.append(piece)
    if len(chunks) > 1 and word_count(chunks[-1]) < min_words // 2:
        last = chunks.pop()
        chunks[-1] = f"{chunks[-1]} {last}"
    return chunks


def similarity(a: str, b: str) -> float:
    na, nb = (re.sub(r"[^a-z0-9 ]", " ", s.lower()) for s in (a, b))
    ta, tb = set(na.split()) - {"the", "a", "of", "and", "to", "in", "how", "why", "what"}, \
        set(nb.split()) - {"the", "a", "of", "and", "to", "in", "how", "why", "what"}
    jaccard = len(ta & tb) / max(1, len(ta | tb))
    return max(SequenceMatcher(None, " ".join(na.split()), " ".join(nb.split())).ratio(), jaccard)


class TopicHistory:
    """Every topic ever started (state/topic_history.json) to prevent duplicates across runs."""

    def __init__(self, path: Path):
        self.path = path
        self.items: list[dict] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def titles(self) -> list[str]:
        return [f"{i['title']} — {i['angle']}" for i in self.items]

    def is_duplicate(self, cand: TopicCandidate, threshold: float) -> bool:
        probe = f"{cand.title} {cand.angle}"
        return any(similarity(probe, f"{i['title']} {i['angle']}") >= threshold for i in self.items)

    def add(self, cand: TopicCandidate, folder: Path) -> None:
        self.items.append({"title": cand.title, "angle": cand.angle, "folder": folder.name,
                           "created_utc": utc_now()})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=2, ensure_ascii=False), encoding="utf-8")


class RunLock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> "RunLock":
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or 0)
                os.kill(pid, 0)
                raise RuntimeError(f"Another autopilot run is active (pid {pid}); lock {self.path}")
            except (ValueError, ProcessLookupError):
                log.warning("removing stale lock", extra={"event": "stale_lock"})
            except PermissionError:
                raise RuntimeError(f"Lock held by another user's process: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# =========================================================================== one video
class StopRequested(Exception):
    pass


class GateClosed(Exception):
    """Pipeline intentionally paused (script approval, needs review)."""


class Video:
    def __init__(self, folder: Path, pilot: "Autopilot"):
        self.folder, self.pilot = folder, pilot
        self.state_path = folder / "state.json"
        self.state: dict = read_json(self.state_path) if self.state_path.exists() else {
            "video_id": folder.name, "status": "IN_PROGRESS", "completed_stages": [], "attempts": {},
            "errors": [], "created_utc": utc_now(), "dry_run": pilot.services.dry_run,
            "script_approved": False}
        self.ledger = CostLedger(folder / "cost_report.json", pilot.config.budget, pilot.run_spend,
                                 pilot.month_spend)
        self.jobs = JobStore(folder / "assets" / "jobs.json")
        self.assets_path = folder / "assets" / "assets.json"

    # ---------------------------------------------------------------- state
    def save(self) -> None:
        self.state["updated_utc"] = utc_now()
        write_json(self.state_path, self.state)

    def done(self, stage: str) -> bool:
        return stage in self.state["completed_stages"]

    def mark(self, stage: str) -> None:
        if stage not in self.state["completed_stages"]:
            self.state["completed_stages"].append(stage)
        self.save()

    def set_status(self, status: Status, reason: str = "") -> None:
        self.state["status"] = status
        self.state["status_reason"] = reason
        self.save()
        log.info("video status", extra={"event": "status", "video": self.folder.name, "status": status,
                                        "reason": reason})

    def assets(self) -> list[Asset]:
        if not self.assets_path.exists():
            return []
        return [Asset.model_validate(a) for a in read_json(self.assets_path)]

    def add_asset(self, asset: Asset) -> None:
        existing = [a for a in self.assets() if a.asset_id != asset.asset_id]
        write_json(self.assets_path, [a.model_dump() for a in existing + [asset]])

    def p(self, *parts: str) -> Path:
        return self.folder.joinpath(*parts)

    def script_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for name in ("script.json", "short_script.json", "storyboard_long.json", "storyboard_short.json"):
            digest.update(self.p(name).read_bytes())
        return digest.hexdigest()


# =========================================================================== orchestrator
class Autopilot:
    def __init__(self, root: Path, config: AutopilotConfig, services: Services, *,
                 outputs: Path, state_dir: Path, catalog: dict,
                 profiles: tuple[RenderProfile, RenderProfile] = (LONG_FORM, SHORT_FORM)):
        self.root, self.config, self.services, self.catalog = root, config, services, catalog
        self.outputs, self.state_dir = outputs, state_dir
        self.long_profile, self.short_profile = profiles
        self.run_spend = RunSpend()
        self.month_spend = MonthlySpend(state_dir)
        self.history = TopicHistory(state_dir / "topic_history.json")
        self.music = MusicLibrary(root / config.music.library_dir)
        self._stop = False

    # ---------------------------------------------------------------- control
    def request_stop(self, *_: object) -> None:
        if self._stop:
            raise KeyboardInterrupt
        self._stop = True
        log.warning("stop requested; finishing current step", extra={"event": "stop_requested"})

    def should_stop(self) -> bool:
        return self._stop

    def _check_stop(self) -> None:
        if self._stop:
            raise StopRequested()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self.request_stop)

    # ---------------------------------------------------------------- run loop
    def resumable_videos(self) -> list[Path]:
        if not self.outputs.exists():
            return []
        found = []
        for state in sorted(self.outputs.glob("*/state.json")):
            data = read_json(state)
            if data.get("status") in RESUMABLE:
                if data["status"] == "AWAITING_SCRIPT_APPROVAL" and not data.get("script_approved"):
                    continue
                found.append(state.parent)
        return found

    def run(self, max_new: int | None = None) -> list[dict]:
        """Resume unfinished videos first, then start new ones up to videos_per_run."""
        results = []
        with RunLock(self.state_dir / "autopilot.lock"):
            for folder in self.resumable_videos():
                results.append(self._process(Video(folder, self)))
                if self._stop or results[-1]["status"] == "PAUSED_BUDGET":
                    return results
            budget = self.config.videos_per_run if max_new is None else max_new
            for _ in range(budget):
                if self._stop:
                    break
                video = self._new_video()
                if video is None:
                    break
                results.append(self._process(video))
                if results[-1]["status"] == "PAUSED_BUDGET":
                    break
        return results

    def _process(self, video: Video) -> dict:
        try:
            if video.state["status"] in ("PAUSED_BUDGET", "AWAITING_SCRIPT_APPROVAL"):
                video.set_status("IN_PROGRESS")
            self.pipeline(video)
        except GateClosed:
            pass
        except StopRequested:
            log.warning("stopped; resumable", extra={"event": "stopped", "video": video.folder.name})
        except KeyboardInterrupt:
            self._stop = True
            log.warning("interrupted; resumable", extra={"event": "stopped", "video": video.folder.name})
        except BudgetExceeded as exc:
            if exc.scope == "video":
                video.set_status("NEEDS_REVIEW", f"per-video budget reached: {exc}")
            else:
                video.set_status("PAUSED_BUDGET", str(exc))
        return {"video": str(video.folder), "status": video.state["status"],
                "reason": video.state.get("status_reason", ""), "cost_usd": round(video.ledger.video_total(), 3)}

    def _stage(self, video: Video, name: str, fn: Callable[[], None]) -> None:
        """Run one idempotent stage with bounded attempts across runs."""
        if video.done(name):
            return
        self._check_stop()
        attempts = video.state["attempts"].get(name, 0)
        if attempts >= self.config.max_stage_attempts:
            video.set_status("FAILED", f"stage {name} failed {attempts} times; see errors")
            raise GateClosed()
        video.state["attempts"][name] = attempts + 1
        video.save()
        log.info("stage start", extra={"event": "stage_start", "video": video.folder.name, "stage": name})
        started = time.monotonic()
        try:
            fn()
        except (GateClosed, StopRequested, KeyboardInterrupt, BudgetExceeded):
            raise
        except Exception as exc:  # recorded for selective retry on the next run
            video.state["errors"].append({"stage": name, "error": f"{type(exc).__name__}: {exc}"[:1500],
                                          "utc": utc_now()})
            video.save()
            log.exception("stage failed", extra={"event": "stage_failed", "video": video.folder.name,
                                                 "stage": name})
            if video.state["attempts"][name] >= self.config.max_stage_attempts:
                video.set_status("FAILED", f"stage {name}: {exc}")
            raise GateClosed() from exc
        video.mark(name)
        log.info("stage done", extra={"event": "stage_done", "video": video.folder.name, "stage": name,
                                      "seconds": round(time.monotonic() - started, 1)})

    def pipeline(self, v: Video) -> None:
        self._stage(v, "research", lambda: self.stage_research(v))
        self._stage(v, "script", lambda: self.stage_script(v))
        self._stage(v, "fact_review", lambda: self.stage_fact_review(v))
        self._stage(v, "storyboard", lambda: self.stage_storyboard(v))
        self._stage(v, "script_gate", lambda: self.stage_script_gate(v))
        self._stage(v, "voice", lambda: self.stage_voice(v))
        self._stage(v, "images", lambda: self.stage_images(v))
        self._stage(v, "motion", lambda: self.stage_motion(v))
        self._stage(v, "thumbnail", lambda: self.stage_thumbnail(v))
        self._stage(v, "render", lambda: self.stage_render(v))
        self._stage(v, "qc", lambda: self.stage_qc(v))
        self._stage(v, "finalize", lambda: self.stage_finalize(v))

    # ---------------------------------------------------------------- stage: topic
    def _new_video(self) -> Video | None:
        stamp = datetime.now().strftime("%Y-%m-%d")
        pending = self.outputs / f"{stamp}_pending-{os.urandom(3).hex()}"
        pending.mkdir(parents=True)
        video = Video(pending, self)
        try:
            candidates = self.services.llm.discover_topics(video.ledger, self.config.niche, self.config.audience,
                                                           self.history.titles())
        except BudgetExceeded as exc:
            video.set_status("PAUSED_BUDGET", str(exc))
            return None
        except Exception as exc:
            video.state["errors"].append({"stage": "topic", "error": str(exc)[:1500], "utc": utc_now()})
            video.set_status("FAILED", f"topic discovery failed: {exc}")
            log.exception("topic discovery failed", extra={"event": "stage_failed", "stage": "topic"})
            return None
        choice = next((c for c in candidates.candidates
                       if not self.history.is_duplicate(c, self.config.dedupe_similarity)), None)
        if choice is None:
            video.set_status("NEEDS_REVIEW", "every proposed topic duplicated earlier videos")
            return None
        final = self.outputs / f"{stamp}_{slugify(choice.title)}"
        n = 2
        while final.exists():  # never overwrite an earlier video folder
            final = self.outputs / f"{stamp}_{slugify(choice.title)}-{n}"
            n += 1
        pending.rename(final)
        video = Video(final, self)
        video.state["video_id"] = final.name
        write_json(video.p("topic.json"), {"chosen": choice.model_dump(),
                                           "candidates": [c.model_dump() for c in candidates.candidates],
                                           "niche": self.config.niche})
        self.history.add(choice, final)
        video.mark("topic")
        return video

    def topic(self, v: Video) -> TopicCandidate:
        return TopicCandidate.model_validate(read_json(v.p("topic.json"))["chosen"])

    # ---------------------------------------------------------------- stage: research
    def stage_research(self, v: Video) -> None:
        topic = self.topic(v)
        brief, seen = self.services.llm.research(v.ledger, topic.title, topic.angle,
                                                 self.config.claude.research_searches)
        # Keep only claims backed by at least one URL actually returned by web search.
        kept = [c for c in brief.claims if any(u in seen for u in c.source_urls)]
        dropped = [c.claim_id for c in brief.claims if c not in kept]
        brief = brief.model_copy(update={"claims": kept})
        if len(kept) < 5:
            raise RuntimeError(f"Only {len(kept)} claims had verifiable sources; topic too thin")
        write_json(v.p("research.json"), {"brief": brief.model_dump(), "seen_urls": sorted(seen),
                                          "dropped_unsourced_claims": dropped})

    def brief(self, v: Video) -> tuple[ResearchBrief, set[str]]:
        data = read_json(v.p("research.json"))
        return ResearchBrief.model_validate(data["brief"]), set(data["seen_urls"])

    # ---------------------------------------------------------------- stage: script
    def stage_script(self, v: Video, feedback: str = "") -> None:
        topic = self.topic(v)
        brief, _ = self.brief(v)
        lo, hi = self.config.long_form_words
        script = self.services.llm.write_script(v.ledger, topic.title, topic.angle, brief, (lo, hi), feedback)
        n = word_count(script.full_narration())
        if not lo * 0.95 <= n <= hi * 1.05:
            script = self.services.llm.write_script(
                v.ledger, topic.title, topic.angle, brief, (lo, hi),
                feedback + f"\nThe previous draft had {n} words; it MUST be {lo}-{hi} words.")
            n = word_count(script.full_narration())
            if not lo * 0.9 <= n <= hi * 1.1:
                raise RuntimeError(f"Long-form script length {n} words outside {lo}-{hi}")
        slo, shi = self.config.short_words
        short = self.services.llm.write_short(v.ledger, script, brief, (slo, shi), feedback)
        sn = word_count(short.narration)
        if not slo * 0.9 <= sn <= shi * 1.05:
            short = self.services.llm.write_short(v.ledger, script, brief, (slo, shi),
                                                  f"The previous draft had {sn} words; it MUST be {slo}-{shi}.")
            sn = word_count(short.narration)
            if not slo * 0.85 <= sn <= shi * 1.1:
                raise RuntimeError(f"Short script length {sn} words outside {slo}-{shi}")
        if len(script.sections) < 3:
            raise RuntimeError("Long-form script needs at least 3 sections for a complete narrative")
        write_json(v.p("script.json"), script)
        write_json(v.p("short_script.json"), short)
        md = [f"# {script.title}", "", f"**Hook:** {script.hook}", ""]
        for i, s in enumerate(script.sections, 1):
            md += [f"## {i}. {s.heading}", "", s.narration, "", f"_claims: {', '.join(s.claim_ids)}_", ""]
        md += ["---", f"# Short: {short.title}", "", short.narration, ""]
        v.p("script.md").write_text("\n".join(md), encoding="utf-8")

    def scripts(self, v: Video) -> tuple[VideoScript, ShortScript]:
        return (VideoScript.model_validate(read_json(v.p("script.json"))),
                ShortScript.model_validate(read_json(v.p("short_script.json"))))

    # ---------------------------------------------------------------- stage: fact review
    def stage_fact_review(self, v: Video) -> None:
        script, short = self.scripts(v)
        brief, _ = self.brief(v)
        review: FactReview = self.services.llm.review_facts(v.ledger, script, short, brief)
        rounds = [review.model_dump()]
        if review.verdict == "revise":  # one automatic correction round, then re-check
            feedback = "Fix these fact-check issues:\n" + "\n".join(
                f"- [{i.severity}] {i.claim_id}: {i.problem}" for i in review.issues)
            self.stage_script(v, feedback)
            script, short = self.scripts(v)
            review = self.services.llm.review_facts(v.ledger, script, short, brief)
            rounds.append(review.model_dump())
        write_json(v.p("fact_review.json"), {"final_verdict": review.verdict, "rounds": rounds})
        if review.verdict != "pass":
            v.mark("fact_review")
            v.set_status("NEEDS_REVIEW", f"fact review verdict {review.verdict}; see fact_review.json")
            raise GateClosed()

    # ---------------------------------------------------------------- stage: storyboard
    def _board(self, v: Video, profile: RenderProfile, title: str, sections: list[tuple[str, str]],
               tag: str, chunk_words: tuple[int, int], motion_cap: int) -> Storyboard:
        shots: list[Shot] = []
        motion_left = motion_cap
        for si, (heading, narration) in enumerate(sections):
            self._check_stop()
            chunks = split_into_chunks(narration, *chunk_words)
            ids = [f"{tag}{si:02}-{j:02}" for j in range(len(chunks))]
            plans = self.services.llm.plan_shots(v.ledger, title, self.config.visual_style, profile.aspect_ratio,
                                                 list(zip(ids, chunks)), max(0, motion_left))
            for sid, text, plan in zip(ids, chunks, plans.shots):
                wants = plan.wants_motion and motion_left > 0
                motion_left -= int(wants)
                shots.append(Shot(shot_id=sid, section_index=si, narration=text,
                                  visual_prompt=plan.visual_prompt, kind="motion" if wants else "still",
                                  camera=plan.camera, motion_prompt=plan.motion_prompt))
        return Storyboard(profile=profile.name, title=title, style=self.config.visual_style,
                          section_headings=[h for h, _ in sections], shots=shots)

    def stage_storyboard(self, v: Video) -> None:
        script, short = self.scripts(v)
        # The hook is spoken inside section 1's narration (the prompt requires it); script.hook is metadata.
        long_sections = [(s.heading, s.narration) for s in script.sections]
        board_long = self._board(v, self.long_profile, script.title, long_sections, "L", (14, 24),
                                 self.config.video.max_motion_shots_long)
        board_short = self._board(v, self.short_profile, short.title, [("Short", short.narration)], "S",
                                  (6, 13), self.config.video.max_motion_shots_short)
        write_json(v.p("storyboard_long.json"), board_long)
        write_json(v.p("storyboard_short.json"), board_short)
        write_json(v.p("storyboard.json"), {"long_form": board_long.model_dump(), "short": board_short.model_dump()})

    def boards(self, v: Video, timed: bool = False) -> tuple[Storyboard, Storyboard]:
        suffix = "_timed" if timed else ""
        return (Storyboard.model_validate(read_json(v.p(f"storyboard_long{suffix}.json"))),
                Storyboard.model_validate(read_json(v.p(f"storyboard_short{suffix}.json"))))

    # ---------------------------------------------------------------- stage: script gate
    def stage_script_gate(self, v: Video) -> None:
        if self.config.script_review == "automated":
            v.state["script_gate"] = "automated: research sources verified + AI fact review passed"
            return
        if v.state.get("script_approved") and v.state.get("approved_fingerprint") == v.script_fingerprint():
            return
        v.state["attempts"]["script_gate"] = 0  # waiting is not a failure
        v.set_status("AWAITING_SCRIPT_APPROVAL",
                     "review script.md, storyboard.json and fact_review.json, then run auto-approve-script")
        raise GateClosed()

    # ---------------------------------------------------------------- stage: voice
    def stage_voice(self, v: Video) -> None:
        for board, tag in zip(self.boards(v), ("long", "short")):
            sections = sorted({s.section_index for s in board.shots})
            texts = [" ".join(s.narration for s in board.shots if s.section_index == i) for i in sections]
            for i, text in enumerate(texts):
                self._check_stop()
                dest = v.p("assets", "voice", f"{tag}_{i:02}.mp3")
                self.services.tts.synthesize(text=text, dest_audio=dest,
                                             previous_text=texts[i - 1] if i else "",
                                             next_text=texts[i + 1] if i + 1 < len(texts) else "",
                                             ledger=v.ledger)
                v.add_asset(Asset(asset_id=f"voice-{tag}-{i:02}", kind="voice",
                                  path=str(dest.relative_to(v.folder)), provider="elevenlabs",
                                  model=self.config.voice.model_id, prompt=text[:200],
                                  license=self.services.licenses.get("elevenlabs", "per ElevenLabs plan terms"),
                                  commercial_use_confirmed=(True if self.config.voice.commercial_license_confirmed
                                                            else None)))

    def _timed_board(self, v: Video, board: Storyboard, tag: str, work: Path) -> tuple[Storyboard, Path, list[dict]]:
        from studio.render import assign_shot_timing, build_narration
        sections = sorted({s.section_index for s in board.shots})
        audio = [v.p("assets", "voice", f"{tag}_{i:02}.mp3") for i in sections]
        alignments = [read_json(p.with_suffix(".alignment.json")) for p in audio]
        narration, starts, lengths = build_narration(audio, work)
        return assign_shot_timing(board, alignments, starts, lengths), narration, alignments

    # ---------------------------------------------------------------- stage: images
    def _image_provider(self) -> str:
        return "dryrun" if self.services.dry_run else "fal"

    def stage_images(self, v: Video) -> None:
        for board, profile in zip(self.boards(v), (self.long_profile, self.short_profile)):
            for shot in board.shots:
                self._check_stop()
                dest = v.p("assets", "images", f"{shot.shot_id}.png")
                prompt = (f"{board.style}. {shot.visual_prompt}. Composition for {profile.aspect_ratio} "
                          "video frame, subject inside safe area. No text, letters, captions, logos or watermarks.")
                record = self.services.images.generate(key=f"image:{shot.shot_id}", prompt=prompt,
                                                       size=profile.image_size, dest=dest, store=v.jobs,
                                                       ledger=v.ledger, should_stop=self.should_stop)
                v.add_asset(Asset(asset_id=f"img-{shot.shot_id}", kind="image",
                                  path=str(dest.relative_to(v.folder)), provider=self._image_provider(),
                                  model=self.services.images.spec["model_id"], request_id=record.request_id,
                                  prompt=prompt, cost_usd=record.estimated_usd,
                                  license=self.services.licenses.get("fal", "fal.ai / model license terms"),
                                  commercial_use_confirmed=None))

    # ---------------------------------------------------------------- stage: motion
    def stage_motion(self, v: Video) -> None:
        """Animate only the selected hero shots, from their approved stills (consistency + cost)."""
        boards = self.boards(v)
        work = v.p("work")
        for board, profile, tag in zip(boards, (self.long_profile, self.short_profile), ("long", "short")):
            timed, _, _ = self._timed_board(v, board, tag, work / f"narration_{tag}")
            for shot in timed.shots:
                if shot.kind != "motion" or (shot.duration_seconds or 0) < self.config.video.min_motion_shot_seconds:
                    continue
                self._check_stop()
                dest = v.p("assets", "clips", f"{shot.shot_id}.mp4")
                if dest.is_file():
                    continue
                req = VideoRequest(shot_id=shot.shot_id, mode="image_to_video",
                                   prompt=f"{shot.motion_prompt or shot.visual_prompt}. Style: {board.style}",
                                   image_path=v.p("assets", "images", f"{shot.shot_id}.png"),
                                   seconds_needed=float(shot.duration_seconds or 5),
                                   aspect_ratio=profile.aspect_ratio, resolution=self.config.video.resolution,
                                   camera=shot.camera)
                try:
                    provider, model, request_id = self.services.make_clip(req, dest, v.jobs, v.ledger,
                                                                          self.should_stop)
                except ProviderError as exc:  # degrade gracefully to an animated still
                    log.warning("motion unavailable; using animated still",
                                extra={"event": "motion_degraded", "shot": shot.shot_id, "error": str(exc)})
                    v.state.setdefault("degraded_shots", []).append(shot.shot_id)
                    v.save()
                    continue
                v.add_asset(Asset(asset_id=f"clip-{shot.shot_id}", kind="video_clip",
                                  path=str(dest.relative_to(v.folder)), provider=provider, model=model,
                                  request_id=request_id, prompt=req.prompt,
                                  license=self.services.licenses.get(provider, "provider/model license terms"),
                                  commercial_use_confirmed=None))

    # ---------------------------------------------------------------- stage: thumbnail
    def stage_thumbnail(self, v: Video) -> None:
        from studio.render import compose_thumbnail
        script, _ = self.scripts(v)
        board, _ = self.boards(v)
        background = v.p("assets", "images", "thumbnail_background.png")
        prompt = (f"{board.style}. {board.shots[0].visual_prompt}. Bold, high-contrast YouTube thumbnail "
                  "composition, single clear focal subject, lower third kept simple. No text, no logos.")
        record = self.services.images.generate(key="image:thumbnail", prompt=prompt,
                                               size={"width": 1280, "height": 720}, dest=background,
                                               store=v.jobs, ledger=v.ledger, should_stop=self.should_stop)
        compose_thumbnail(background, script.thumbnail_text, v.p("thumbnail.png"))
        v.add_asset(Asset(asset_id="thumbnail", kind="thumbnail", path="thumbnail.png", provider=self._image_provider(),
                          model=self.services.images.spec["model_id"], request_id=record.request_id,
                          prompt=prompt, license=self.services.licenses.get("fal", "fal.ai / model license terms"),
                          commercial_use_confirmed=None,
                          notes=f"text overlay: {script.thumbnail_text!r} (local PIL)"))

    # ---------------------------------------------------------------- stage: render
    def _music_for(self, v: Video) -> Path | None:
        recent = [i.get("music") for i in self._recent_finals()]
        track = self.music.pick(v.folder.name, [r for r in recent if r])
        if track is None:
            return None
        v.add_asset(Asset(asset_id="music", kind="music", path=str(self.music.dir / track.file),
                          provider="user_library", model=track.title, license=track.license,
                          commercial_use_confirmed=track.commercial_use_confirmed, notes=track.source_url))
        v.state["music"] = track.file
        v.save()
        return self.music.dir / track.file

    def _recent_finals(self) -> list[dict]:
        return [read_json(p) for p in sorted(self.outputs.glob("*/state.json"))[-10:]]

    def output_name(self, v: Video, profile: RenderProfile) -> Path:
        slug = slugify(self.topic(v).title, 50)
        return v.p(f"{slug}__{profile.filename_suffix}.mp4")

    def stage_render(self, v: Video) -> None:
        from studio.render import render_video
        music = self._music_for(v)
        for board, profile, tag in zip(self.boards(v), (self.long_profile, self.short_profile), ("long", "short")):
            self._check_stop()
            timed, narration, alignments = self._timed_board(v, board, tag, v.p("work", f"narration_{tag}"))
            write_json(v.p(f"storyboard_{tag}_timed.json"), timed)
            words = []
            for shot_sec, align in zip(sorted({s.section_index for s in timed.shots}), alignments):
                offset = next(s.start_seconds for s in timed.shots if s.section_index == shot_sec) or 0.0
                words += words_from_alignment(align, offset)
            cues = build_cues(words, profile.caption_words_per_cue)
            srt = write_srt(cues, v.p(f"captions_{tag}.srt"))
            ass = write_ass(cues, v.p("work", f"captions_{tag}.ass"), profile)
            sources: dict[str, tuple[Path, bool]] = {}
            for shot in timed.shots:
                clip = v.p("assets", "clips", f"{shot.shot_id}.mp4")
                sources[shot.shot_id] = (clip, True) if clip.is_file() else \
                    (v.p("assets", "images", f"{shot.shot_id}.png"), False)
            render_video(folder=v.folder, board=timed, sources=sources, narration=narration, profile=profile,
                         music=music, music_db=self.config.music.volume_db,
                         captions_ass=ass if profile.burn_captions else None,
                         dest=self.output_name(v, profile))
            v.add_asset(Asset(asset_id=f"captions-{tag}", kind="captions", path=srt.name, provider="local",
                              model="elevenlabs-alignment", license="generated locally from own narration",
                              commercial_use_confirmed=True))

    # ---------------------------------------------------------------- stage: qc
    def stage_qc(self, v: Video) -> None:
        from studio import qc
        from studio.render import extract_frames
        script, short = self.scripts(v)
        brief, seen = self.brief(v)
        fact = read_json(v.p("fact_review.json"))
        used = {c for s in script.sections for c in s.claim_ids} | set(short.claim_ids)
        shared = (qc.factual_checks(brief, seen, used, fact["final_verdict"])
                  + qc.provenance_checks(v.assets(), self.config.voice.commercial_license_confirmed,
                                         music_required=True))
        reports = {}
        for board, profile, tag, narration in zip(self.boards(v, timed=True), (self.long_profile, self.short_profile),
                                                  ("long", "short"),
                                                  (" ".join(b.narration for b in self.boards(v)[0].shots),
                                                   " ".join(b.narration for b in self.boards(v)[1].shots))):
            video = self.output_name(v, profile)
            checks = qc.technical_checks(video, profile)
            duration = float(board.total_seconds or 0)
            checks += qc.caption_checks(v.p(f"captions_{tag}.srt"), word_count(narration), duration)
            sections = len({s.section_index for s in board.shots})
            checks += qc.narrative_checks(board, narration, sections)
            checks += qc.repeated_visual_checks([v.p("assets", "images", f"{s.shot_id}.png") for s in board.shots])
            checks += shared
            if self.config.ai_frame_review:
                frames = extract_frames(video, 8 if tag == "long" else 5, v.p("work", f"frames_{tag}"))
                review = self.services.llm.review_frames(v.ledger, board.title, frames)
                status = "pass" if review.verdict == "pass" and min(review.visual_quality_score,
                                                                    review.style_consistency_score) >= 6 else "warn"
                checks.append(qc.Check(name="ai_frame_review", status=status,
                                       detail=f"quality {review.visual_quality_score}/10, consistency "
                                              f"{review.style_consistency_score}/10; {'; '.join(review.issues)[:400]}"))
            report = qc.QCReport(profile=profile.name, video=video.name, checks=checks)
            reports[tag] = report.summary()
        write_json(v.p("qc_report.json"), reports)

    # ---------------------------------------------------------------- stage: finalize
    def stage_finalize(self, v: Video) -> None:
        script, short = self.scripts(v)
        reports = read_json(v.p("qc_report.json"))
        passed = all(r["passed"] for r in reports.values())
        warnings = sum(len(r["warnings"]) for r in reports.values())
        metadata = {
            "status_note": "NOT PUBLISHED. Upload manually after human review.",
            "long_form": {"file": self.output_name(v, self.long_profile).name, "title": script.title,
                          "description": script.youtube_description, "tags": script.tags,
                          "captions_file": "captions_long.srt", "thumbnail": "thumbnail.png"},
            "short": {"file": self.output_name(v, self.short_profile).name, "title": short.title,
                      "description": short.description, "tags": short.tags, "captions": "burned in"},
            "ai_disclosure": {
                "contains_ai_generated_visuals": True, "contains_synthetic_voice": True,
                "youtube_altered_or_synthetic_content": "Answer YES in YouTube Studio > Details > "
                    "'Altered content' when visuals look realistic (YouTube policy: support.google.com/youtube/answer/14328491).",
                "suggested_description_line": "Visuals and narration were produced with AI tools; facts were researched and cited below.",
            },
            "sources": sorted({u for c in self.brief(v)[0].claims for u in c.source_urls}),
            "monetization_note": "Eligibility is decided by YouTube. Automated checks cannot guarantee it.",
            "degraded_motion_shots": v.state.get("degraded_shots", []),
            "dry_run": self.services.dry_run,
        }
        write_json(v.p("metadata.json"), metadata)
        write_json(v.p("provenance.json"), [a.model_dump() for a in v.assets()])
        if passed:
            v.set_status("COMPLETE_PENDING_HUMAN_REVIEW",
                         f"all automated checks passed ({warnings} warning(s)); human review required")
        else:
            v.mark("finalize")
            v.set_status("NEEDS_REVIEW", "automated QC failed; see qc_report.json")


# =========================================================================== CLI helpers
def approve_script(folder: Path, confirm_reviewed: bool) -> str:
    if not confirm_reviewed:
        raise ValueError("Read script.md, storyboard.json and fact_review.json, then pass --confirm-reviewed")
    state_path = folder / "state.json"
    state = read_json(state_path)
    if state.get("status") != "AWAITING_SCRIPT_APPROVAL":
        raise ValueError(f"Video is {state.get('status')}, not awaiting script approval")
    digest = hashlib.sha256()
    for name in ("script.json", "short_script.json", "storyboard_long.json", "storyboard_short.json"):
        digest.update((folder / name).read_bytes())
    state.update(script_approved=True, approved_fingerprint=digest.hexdigest(), approved_utc=utc_now())
    write_json(state_path, state)
    return digest.hexdigest()


def status_table(outputs: Path, state_dir: Path) -> dict:
    videos = []
    for state in sorted(outputs.glob("*/state.json")):
        data = read_json(state)
        cost_path = state.parent / "cost_report.json"
        cost = read_json(cost_path)["video_total_usd"] if cost_path.exists() else 0.0
        videos.append({"folder": state.parent.name, "status": data.get("status"),
                       "reason": data.get("status_reason", ""), "stages_done": len(data.get("completed_stages", [])),
                       "cost_usd": cost})
    return {"month_to_date_usd": round(MonthlySpend(state_dir).total(), 3), "videos": videos}
