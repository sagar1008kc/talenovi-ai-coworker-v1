"""Structured data for the autopilot pipeline: render profiles, storyboards, shots and assets.

LLM output schemas live here too. They deliberately have no defaults or numeric/string
constraints, because Claude structured outputs require every field to be listed as required
and do not support min/max constraints; length rules are validated in Python instead.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- render profiles
class RenderProfile(BaseModel):
    """One independently rendered and validated output format."""
    model_config = ConfigDict(frozen=True)

    name: Literal["long_16x9", "short_9x16"]
    width: int
    height: int
    fps: int = 30
    min_seconds: float
    max_seconds: float
    filename_suffix: str
    aspect_ratio: Literal["16:9", "9:16"]
    burn_captions: bool
    caption_words_per_cue: int
    image_size: dict[str, int]  # custom FLUX size; multiples of 16, cropped to width x height
    crf: int = 20

    def scaled(self, divisor: int) -> "RenderProfile":
        """Small, fast variant for offline dry runs. Duration rules are unchanged."""
        even = lambda v: max(2, (v // divisor) // 2 * 2)  # noqa: E731 - libx264 needs even sizes
        return self.model_copy(update={"width": even(self.width), "height": even(self.height)})


LONG_FORM = RenderProfile(
    name="long_16x9", width=1920, height=1080, min_seconds=456, max_seconds=630,
    filename_suffix="full_16x9", aspect_ratio="16:9", burn_captions=False,
    caption_words_per_cue=7, image_size={"width": 1920, "height": 1088},
)
SHORT_FORM = RenderProfile(
    name="short_9x16", width=1080, height=1920, min_seconds=30, max_seconds=60,
    filename_suffix="short_9x16", aspect_ratio="9:16", burn_captions=True,
    caption_words_per_cue=3, image_size={"width": 1088, "height": 1920},
)
PROFILES = {LONG_FORM.name: LONG_FORM, SHORT_FORM.name: SHORT_FORM}

CameraMove = Literal["zoom_in", "zoom_out", "pan_left", "pan_right", "push_slow"]


# --------------------------------------------------------------------------- storyboard
class Shot(BaseModel):
    shot_id: str
    section_index: int
    narration: str
    visual_prompt: str
    kind: Literal["still", "motion"] = "still"
    camera: CameraMove = "push_slow"
    motion_prompt: str = ""
    start_seconds: float | None = None
    duration_seconds: float | None = None
    image_asset_id: str | None = None
    clip_asset_id: str | None = None


class Storyboard(BaseModel):
    profile: str
    title: str
    style: str
    section_headings: list[str]
    shots: list[Shot]
    total_seconds: float | None = None

    def narration_text(self) -> str:
        return " ".join(s.narration for s in self.shots)


class Asset(BaseModel):
    """Provenance record for every generated or licensed file."""
    asset_id: str
    kind: Literal["image", "video_clip", "voice", "music", "thumbnail", "captions"]
    path: str  # relative to the video folder
    provider: str
    model: str
    request_id: str | None = None
    prompt: str = ""
    cost_usd: float = 0.0
    license: str
    commercial_use_confirmed: bool | None = None  # None = must be checked by a human
    created_utc: str = Field(default_factory=utc_now)
    notes: str = ""


# --------------------------------------------------------------------------- LLM output schemas
class TopicCandidate(BaseModel):
    title: str
    angle: str
    audience_value: str
    why_now: str


class TopicCandidates(BaseModel):
    candidates: list[TopicCandidate]


class Claim(BaseModel):
    claim_id: str
    statement: str
    source_urls: list[str]
    confidence: Literal["high", "medium", "low"]


class ResearchBrief(BaseModel):
    summary: str
    claims: list[Claim]
    open_questions: list[str]


class ScriptSection(BaseModel):
    heading: str
    narration: str
    claim_ids: list[str]


class VideoScript(BaseModel):
    title: str
    hook: str
    sections: list[ScriptSection]
    youtube_description: str
    tags: list[str]
    thumbnail_text: str  # 2-5 honest words for the thumbnail

    def full_narration(self) -> str:
        return " ".join(s.narration for s in self.sections)


class ShortScript(BaseModel):
    title: str
    narration: str
    claim_ids: list[str]
    description: str
    tags: list[str]


class ShotPlan(BaseModel):
    shot_id: str
    visual_prompt: str
    wants_motion: bool
    camera: CameraMove
    motion_prompt: str


class ShotPlans(BaseModel):
    shots: list[ShotPlan]


class FactIssue(BaseModel):
    claim_id: str
    problem: str
    severity: Literal["minor", "major"]


class FactReview(BaseModel):
    verdict: Literal["pass", "revise", "fail"]
    issues: list[FactIssue]
    notes: str


class FrameReview(BaseModel):
    visual_quality_score: int
    style_consistency_score: int
    issues: list[str]
    verdict: Literal["pass", "needs_review"]
