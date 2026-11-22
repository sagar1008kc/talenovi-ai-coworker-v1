"""Deterministic preflight checks; not a replacement for editorial/legal review."""
from __future__ import annotations

from difflib import SequenceMatcher
import re
from pathlib import Path
from typing import Any

from studio.series import read_json

from studio.series import SeriesBible, ContinuityLedger

# This is deliberately a small obvious-language filter, not a universal profanity detector.
# Context, translations, and subtle violations still require human editorial review.
BLOCKED_WORDS = {"fuck", "fucking", "shit", "bitch", "cunt", "motherfucker"}


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.casefold())).strip()


def preflight(story: Any, bible: SeriesBible, ledger: ContinuityLedger, root: Path | None = None) -> dict:
    """Fail closed on broken series references and high-similarity scripts."""
    errors: list[str] = []
    warnings: list[str] = []

    if not bible.cast_approved:
        errors.append("Recurring cast and story world must be human-approved before episode production")
    if story.series_slug != bible.slug:
        errors.append("Episode series ID does not match the series bible")
    expected = ledger.last_approved_episode + 1
    if story.episode_number != expected:
        errors.append(f"Episode number must be {expected} after approved canon")

    character_ids = {c.id for c in bible.characters}
    unknown = set(story.character_ids) - character_ids
    if unknown:
        errors.append(f"Unregistered characters: {sorted(unknown)}")

    known_voices = set(bible.voices)
    for index, scene in enumerate(story.scenes, start=1):
        if scene.speaker_id not in known_voices:
            errors.append(f"Scene {index} has unknown voice {scene.speaker_id!r}")
        unknown_scene_characters = set(scene.character_ids) - character_ids
        if unknown_scene_characters:
            errors.append(f"Scene {index} unknown characters: {sorted(unknown_scene_characters)}")
        if set(scene.character_ids) - set(story.character_ids):
            errors.append(f"Scene {index} characters absent from episode cast")

    corpus = " ".join([
        story.title, story.hook, story.summary, story.narration, story.youtube_description,
        *[scene.visual_prompt for scene in story.scenes],
    ])
    for term in sorted(set(normalized(corpus).split()) & BLOCKED_WORDS):
        errors.append(f"Family-safe language rule: blocked term '{term}'")

    # Intra-library similarity is useful, but cannot establish internet-wide originality.
    for previous in ledger.approved_episodes:
        score = SequenceMatcher(None, normalized(story.narration), normalized(previous.story_text)).ratio()
        if score >= 0.78:
            errors.append(f"Too similar to approved episode {previous.number}: {score:.0%}")
        elif score >= 0.58:
            warnings.append(f"Check possible similarity with episode {previous.number}: {score:.0%}")

    # Optional project-wide similarity scan catches reused approved plots across series.
    # It cannot search the web or establish that a script is unique worldwide.
    if root is not None:
        series_dir = root / "series"
        for candidate in series_dir.glob("*/continuity.json"):
            if candidate.parent.name == bible.slug:
                continue
            for old in read_json(candidate).get("approved_episodes", []):
                other_text = str(old.get("story_text", ""))
                if not other_text:
                    continue
                score = SequenceMatcher(None, normalized(story.narration), normalized(other_text)).ratio()
                identity = f"{candidate.parent.name} episode {old.get('number', '?')}"
                if score >= 0.78:
                    errors.append(f"Too similar to different series {identity}: {score:.0%}")
                elif score >= 0.58:
                    warnings.append(f"Check similarity to different series {identity}: {score:.0%}")

    if len(story.narration.split()) < 110:
        warnings.append("Short narration: confirm target duration with actual generated audio")
    if not story.emotional_turn:
        warnings.append("No explicit emotional turn; human editorial review needed")
    if not story.ending_payoff:
        warnings.append("No explicit payoff; avoid an empty cliffhanger")
    if not story.canon_updates:
        warnings.append("No canon updates provided; confirm this episode truly changes nothing")

    if bible.season_outline and story.episode_number:
        scheduled = next((b for b in bible.season_outline if b.number == story.episode_number), None)
        if scheduled is None:
            warnings.append("Episode is outside the approved season outline; request editorial approval")
        else:
            warnings.append("Human should confirm episode matches approved season beat and emotional payoff")

    return {"passed": not errors, "errors": errors, "warnings": warnings,
            "requires_human_editorial_review": True,
            "originality_scope": "Checks approved scripts in this series and optionally the local project; not the internet"}
