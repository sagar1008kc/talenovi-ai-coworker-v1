"""Typed records for the motion-first director pipeline: show bible and episode shotlist.

Layout of one show (all paths relative to shows/<show_slug>/):
    bible/characters/<char_id>/character.json  + refs/*.png   (canonical turnaround / reference images)
    bible/locations/<loc_id>/location.json     + plates/*.png (background plates, lighting)
    bible/voices.json                          (character_id -> locked ElevenLabs voice)
    bible/bible.lock.json                      (SHA-256 fingerprint of everything above)
    episodes/epXX/shotlist.json                (the episode's state machine)
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
SHOT_RE = re.compile(r"^S\d{2}[A-Z]?$")
ShotStatus = Literal["pending", "rendered", "qa_failed", "approved"]
CameraMove = Literal["static_locked", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down",
                     "dolly_left", "dolly_right", "orbit_left", "orbit_right", "handheld", "crane_up", "crane_down",
                     "tracking"]
Transition = Literal["cut", "fade_in", "fade_out", "fadeblack"]


def _check_id(value: str, what: str) -> str:
    if not ID_RE.match(value):
        raise ValueError(f"{what} {value!r} must be lowercase snake_case (2-40 chars)")
    return value


class Character(BaseModel):
    character_id: str
    name: str
    age: str                                  # "31", "about 8", "ageless fruit"
    look: str                                 # face, hair, build: restated in every motion prompt
    wardrobe: str                             # default outfit (signature colours / props)
    traits: list[str] = Field(default_factory=list)
    style: str = "photoreal cinematic"        # or e.g. "3D animated, soft clay look"
    refs: list[str] = Field(min_length=1)     # files in refs/, best first; front view first

    @field_validator("character_id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v, "character_id")


class Location(BaseModel):
    location_id: str
    name: str
    description: str
    lighting: str
    ambience: str = "night"                   # procedural room tone name or a file in the location folder
    plates: list[str] = Field(min_length=1)   # files in plates/

    @field_validator("location_id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v, "location_id")


class VoiceEntry(BaseModel):
    """A locked voice. The ElevenLabs voice ID itself stays in .env (never in the repo):
    `voice_env_key` names it and the lock stores only its SHA-256, so a swapped ID is detected."""
    voice_env_key: str = Field(pattern=r"^ELEVENLABS_[A-Z0-9_]+_VOICE_ID$")
    model_id: str = "eleven_multilingual_v2"
    stability: float = Field(0.5, ge=0, le=1)
    similarity_boost: float = Field(0.8, ge=0, le=1)
    style: float = Field(0.2, ge=0, le=1)
    speed: float = Field(1.0, ge=0.7, le=1.2)


class SfxCue(BaseModel):
    file: str                                 # path under the show (bible/sfx/...) or assets/sfx/...
    at_sec: float = Field(0.0, ge=0)          # offset inside the shot
    gain_db: float = 0.0


class Shot(BaseModel):
    shot_id: str
    duration_sec: float
    character_ids: list[str] = Field(default_factory=list)
    location_id: str
    camera_movement: CameraMove
    framing: str = "medium"                   # wide | medium | close | extreme_close | insert | over_shoulder
    action: str                               # the one visible thing that happens (acting + environment motion)
    dialogue_line: str = ""
    speaker_id: str | None = None
    voice_id: str | None = None               # = the speaker's voice_env_key from bible/voices.json
    lip_sync_required: bool = False
    sfx_cues: list[SfxCue] = Field(default_factory=list)
    music_cue: str = ""                       # "" = continue, "start:<mood>", "stop", "swell"
    transition_in: Transition = "cut"
    caption_zone: Literal["mid", "high"] = "mid"         # 66% height, or 30% when faces/hands sit low
    wide_focus_y: float | None = Field(None, ge=0, le=1)   # 16:9 band centre when no face is detected
    motion_model: str | None = None           # override director.motion_model (catalog key), e.g. a pilot A/B
    status: ShotStatus = "pending"
    attempts: int = 0
    qa: dict = Field(default_factory=dict)
    assets: dict = Field(default_factory=dict)
    cost_usd: float = 0.0
    history: list[dict] = Field(default_factory=list)

    @field_validator("shot_id")
    @classmethod
    def _sid(cls, v: str) -> str:
        if not SHOT_RE.match(v):
            raise ValueError(f"shot_id {v!r} must look like S01 or S01A")
        return v

    @model_validator(mode="after")
    def _dialogue(self) -> "Shot":
        if self.dialogue_line.strip():
            if not self.speaker_id or not self.voice_id:
                raise ValueError(f"{self.shot_id}: a dialogue_line needs speaker_id and voice_id")
            if self.speaker_id not in self.character_ids and self.lip_sync_required:
                raise ValueError(f"{self.shot_id}: lip-synced speaker must be in character_ids (on camera)")
        elif self.lip_sync_required:
            raise ValueError(f"{self.shot_id}: lip_sync_required without a dialogue_line")
        return self


class Shotlist(BaseModel):
    show_slug: str
    episode_number: int = Field(ge=1, le=99)
    title: str
    logline: str
    music: dict = Field(default_factory=dict)          # {"source": "ai"|"library"|"none", "prompt"|"file": ...}
    pilot: bool = False                                # test episode: any length, never approved or uploaded
    youtube_description: str = ""
    tags: list[str] = Field(default_factory=list)
    canon_updates: list[str] = Field(default_factory=list)          # facts this episode makes true
    character_knowledge: dict[str, str] = Field(default_factory=dict)  # character_id -> what they know now
    next_episode_starts: str = ""
    shots: list[Shot]

    @model_validator(mode="after")
    def _unique(self) -> "Shotlist":
        ids = [s.shot_id for s in self.shots]
        if len(ids) != len(set(ids)):
            raise ValueError("shot_id values must be unique")
        return self

    @property
    def runtime(self) -> float:
        return round(sum(s.duration_sec for s in self.shots), 3)
