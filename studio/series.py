"""Persistent, locally stored series canon, casting approvals, and voice profiles."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class VoiceProfile(BaseModel):
    """Stable speaker configuration. A real, licensed voice ID lives in .env."""
    env_key: str
    model_id: str = "eleven_multilingual_v2"
    stability: float = Field(default=0.55, ge=0, le=1)
    similarity_boost: float = Field(default=0.75, ge=0, le=1)
    style: float = Field(default=0.0, ge=0, le=1)
    speed: float = Field(default=1.0, ge=0.7, le=1.2)
    accent: str = ""
    voice_direction: str = ""

    @field_validator("env_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if not re.fullmatch(r"ELEVENLABS_[A-Z0-9_]*VOICE_ID", value):
            raise ValueError("Voice env_key must be an ELEVENLABS_*VOICE_ID name, not a key or ID")
        return value


class Character(BaseModel):
    id: str
    name: str
    age_range: str
    appearance: str
    signature_wardrobe: str
    temperament: str
    core_desire: str
    fear_or_wound: str
    speech_style: str
    relationships: dict[str, str] = Field(default_factory=dict)
    reference_images: list[str] = Field(default_factory=list)
    internal_need: str = ""
    flaw: str = ""
    emotional_triggers: list[str] = Field(default_factory=list)
    personality_arc: str = ""
    signature_prop: str = ""
    character_approval_notes: str = ""
    # Story-folder (v6) fields. None = not set; omitted from the fingerprint so older locks stay valid.
    deflection_example: str | None = None   # a short line showing how they dodge a hard question

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
            raise ValueError("Character IDs must be lowercase snake_case")
        return value


class SeasonBeat(BaseModel):
    number: int = Field(ge=1)
    premise: str
    emotional_progression: str
    distinct_payoff: str
    forward_question: str = ""
    # Causal season plan (v6, story-folder stories). None = not set; omitted from the fingerprint.
    lead_want: str | None = None
    lead_choice: str | None = None
    immediate_payoff: str | None = None
    consequence_for_next: str | None = None


class StoryIdentity(BaseModel):
    """Per-story creative identity (v6). Lives inside the bible so it locks with the cast."""
    planned_episodes: int = Field(default=5, ge=5, le=6)
    setting: str = ""
    visual_palette: str = ""
    camera_style: str = ""
    pacing: str = ""
    sound: str = ""
    humor_or_emotional_tone: str = ""
    finale_question: str = ""


class SeriesBible(BaseModel):
    slug: str
    title: str
    premise: str
    genre: list[str]
    tone: str
    target_audience: str
    visual_style: str
    content_rating: str = "PG"
    world_rules: list[str]
    season_goal: str
    story_principles: list[str]
    characters: list[Character]
    voices: dict[str, VoiceProfile]
    season_outline: list[SeasonBeat] = Field(default_factory=list)
    cast_approved: bool = False
    cast_version: int = 0
    cast_approved_at_utc: str | None = None
    identity: StoryIdentity | None = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("Series slug must be lowercase kebab-case")
        return value

    @model_validator(mode="after")
    def validate_cast(self) -> "SeriesBible":
        ids = [c.id for c in self.characters]
        names = [c.name.casefold() for c in self.characters]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ValueError("Character IDs and names must be distinct")
        if "narrator" not in self.voices:
            raise ValueError("Every series needs a narrator voice profile")
        missing = set(ids) - set(self.voices)
        if missing:
            raise ValueError(f"No voice profile for characters: {sorted(missing)}")
        unknown = set(self.voices) - set(ids) - {"narrator"}
        if unknown:
            raise ValueError(f"Voice IDs without a matching character: {sorted(unknown)}")
        for c in self.characters:
            if set(c.relationships) - set(ids):
                raise ValueError(f"{c.id} has relationships referencing unknown characters")
        episode_numbers = [beat.number for beat in self.season_outline]
        if len(set(episode_numbers)) != len(episode_numbers):
            raise ValueError("Season outline episode numbers must be unique")
        return self


class ApprovedEpisode(BaseModel):
    number: int
    title: str
    summary: str
    story_text: str
    canon_updates: list[str] = Field(default_factory=list)
    unresolved_threads: list[str] = Field(default_factory=list)
    character_knowledge: dict[str, list[str]] = Field(default_factory=dict)  # what each character now knows
    next_episode_starts: str = ""                                         # where the next episode picks up
    approved_at_utc: str


class ContinuityLedger(BaseModel):
    last_approved_episode: int = 0
    approved_episodes: list[ApprovedEpisode] = Field(default_factory=list)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement avoids a half-written series bible / canon file.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def series_path(root: Path, slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Invalid series slug")
    return root / "series" / slug


# Fields added after the first locks. When unset (None) they are left out of the fingerprint, so bibles
# locked before they existed keep their original hash; once set, they are fingerprinted like everything else.
_OPTIONAL_TOP = ("identity",)
_OPTIONAL_CHARACTER = ("deflection_example",)
_OPTIONAL_BEAT = ("lead_want", "lead_choice", "immediate_payoff", "consequence_for_next")


def _fingerprint_view(bible: SeriesBible) -> dict[str, Any]:
    data = bible.model_dump(mode="json")
    for key in _OPTIONAL_TOP:
        if data.get(key) is None:
            data.pop(key, None)
    for group, keys in (("characters", _OPTIONAL_CHARACTER), ("season_outline", _OPTIONAL_BEAT)):
        for item in data.get(group, []):
            for key in keys:
                if item.get(key) is None:
                    item.pop(key, None)
    return data


def bible_hash(bible: SeriesBible) -> str:
    """Fingerprint all approved cast, voices and story-world facts."""
    stable = json.dumps(_fingerprint_view(bible), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def get_series(root: Path, slug: str) -> tuple[SeriesBible, ContinuityLedger]:
    folder = series_path(root, slug)
    bible = SeriesBible.model_validate(read_json(folder / "series_bible.json"))
    if bible.slug != slug:
        raise ValueError("Series folder and bible slug do not match")
    ledger = ContinuityLedger.model_validate(read_json(folder / "continuity.json"))
    lock_path = folder / "cast_lock.json"
    if bible.cast_approved:
        if not lock_path.exists():
            raise ValueError("Approved cast has no lock file. Restore or reapprove explicitly")
        recorded_hash = read_json(lock_path).get("sha256")
        if recorded_hash != bible_hash(bible):
            raise ValueError("Approved series bible has changed without recasting approval")
    elif lock_path.exists():
        raise ValueError("Cast lock exists but bible is not marked approved")
    return bible, ledger


def create_series(root: Path, slug: str, *, story_folder: bool = False, planned_episodes: int | None = None,
                  title: str | None = None) -> Path:
    """Create a series. story_folder=True is the v6 layout: series/<slug>/{episodes,references}/."""
    folder = series_path(root, slug)
    if folder.exists():
        raise FileExistsError(f"Series already exists: {folder}")
    template = read_json(root / "examples" / "series_bible.template.json")
    template["slug"] = slug
    template["title"] = slug.replace("-", " ").title()
    template["cast_approved"] = False
    template["cast_version"] = 0
    template["cast_approved_at_utc"] = None
    if title:
        template["title"] = title
    if story_folder:
        template["identity"] = StoryIdentity(planned_episodes=planned_episodes or 5).model_dump(mode="json")
    bible = SeriesBible.model_validate(template)
    folder.mkdir(parents=True)
    write_json(folder / "series_bible.json", bible.model_dump(mode="json"))
    write_json(folder / "continuity.json", ContinuityLedger().model_dump())
    if story_folder:
        (folder / "episodes").mkdir()
        (folder / "references").mkdir()
        write_json(folder / "references" / "manifest.json", {
            "about": "Approved character and recurring-prop references for this story only. Entries point at "
                     "accepted episode assets (stored once); nothing here is stock, web or real-person media.",
            "characters": {}, "props": {}})
        return folder
    (folder / "reference_images").mkdir(exist_ok=True)
    (folder / "reference_images" / "README.md").write_text(
        "Store original or licensed references. Record rights and use a reference-conditioned "
        "provider before claiming stable faces. Do not upload private likenesses without consent.\n",
        encoding="utf-8",
    )
    return folder


def create_cast_template(root: Path, slug: str) -> Path:
    """Offline proposal workflow; lets users refine examples without API spend."""
    bible, ledger = get_series(root, slug)
    if bible.cast_approved or ledger.last_approved_episode:
        raise ValueError("Cannot replace approved cast; start a new series for a new cast")
    path = series_path(root, slug) / "cast_proposal.json"
    if path.exists():
        raise FileExistsError("Cast proposal already exists; review it instead of overwriting")
    proposal = bible.model_copy(update={"cast_approved": False, "cast_version": 0, "cast_approved_at_utc": None})
    write_json(path, proposal.model_dump(mode="json"))
    return path


def approve_cast(root: Path, slug: str) -> Path:
    """Human-initiated action to lock the full proposed story world and cast."""
    bible, ledger = get_series(root, slug)
    if bible.cast_approved or ledger.last_approved_episode:
        raise ValueError("Approved series cannot be silently recast")
    folder = series_path(root, slug)
    proposal = SeriesBible.model_validate(read_json(folder / "cast_proposal.json"))
    if proposal.slug != slug:
        raise ValueError("Cast proposal series slug does not match folder")
    if not (2 <= len(proposal.characters) <= 5):
        raise ValueError("Cast approval requires 2–5 developed recurring characters")
    if (folder / "episodes").is_dir():  # v6 story-folder layout
        problems = story_contract_errors(proposal)
        if problems:
            raise ValueError("Story contract incomplete: " + "; ".join(problems))
    approved = proposal.model_copy(update={
        "cast_approved": True,
        "cast_version": 1,
        "cast_approved_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    write_json(folder / "series_bible.json", approved.model_dump(mode="json"))
    write_json(folder / "cast_lock.json", {
        "sha256": bible_hash(approved),
        "version": approved.cast_version,
        "approved_at_utc": approved.cast_approved_at_utc,
        "approval_scope": "Full series bible (cast, voices, season arc and world rules)",
    })
    return folder / "cast_lock.json"


def record_approval(root: Path, story: Any) -> None:
    """Append canon ONLY after human approval of a finished video."""
    if not story.series_slug:
        return
    bible, ledger = get_series(root, story.series_slug)
    if not bible.cast_approved:
        raise ValueError("Cast must be explicitly approved before episode approval")
    expected = ledger.last_approved_episode + 1
    if story.episode_number != expected:
        raise ValueError(f"Cannot approve out of sequence: expected episode {expected}")
    folder = series_path(root, bible.slug)
    ledger.approved_episodes.append(ApprovedEpisode(
        number=expected,
        title=story.title,
        summary=story.summary,
        story_text=story.narration,
        canon_updates=story.canon_updates,
        unresolved_threads=story.unresolved_threads,
        character_knowledge=getattr(story, "character_knowledge", {}) or {},
        next_episode_starts=getattr(story, "next_episode_starts", "") or "",
        approved_at_utc=datetime.now(timezone.utc).isoformat(),
    ))
    ledger.last_approved_episode = expected
    write_json(folder / "continuity.json", ledger.model_dump(mode="json"))


ROOT_SEASON_KEYS = ("lead_want", "lead_choice", "immediate_payoff", "consequence_for_next")
IDENTITY_KEYS = ("setting", "visual_palette", "camera_style", "pacing", "sound", "humor_or_emotional_tone",
                 "finale_question")


def story_contract_errors(bible: SeriesBible) -> list[str]:
    """v6 story-folder contract, checked before a cast lock: identity, 5-6 causal parts, voiced characters."""
    errors: list[str] = []
    ident = bible.identity
    if ident is None:
        return ["Story-folder stories need an 'identity' block (setting, palette, camera, pacing, sound, tone, "
                "finale question, planned_episodes)"]
    errors += [f"identity.{k} is empty" for k in IDENTITY_KEYS if not getattr(ident, k).strip()]
    numbers = sorted(b.number for b in bible.season_outline)
    if numbers != list(range(1, ident.planned_episodes + 1)):
        errors.append(f"season_outline must hold episodes 1..{ident.planned_episodes} exactly, got {numbers}")
    for beat in bible.season_outline:
        for key in ROOT_SEASON_KEYS:
            if not (getattr(beat, key) or "").strip():
                errors.append(f"season_outline episode {beat.number}: {key} is empty")
    for c in bible.characters:
        for key in ("core_desire", "fear_or_wound", "appearance", "signature_wardrobe", "speech_style"):
            if not getattr(c, key).strip():
                errors.append(f"character {c.id}: {key} is empty")
        if not (c.deflection_example or "").strip():
            errors.append(f"character {c.id}: deflection_example is empty (how they avoid a difficult question)")
    return errors
