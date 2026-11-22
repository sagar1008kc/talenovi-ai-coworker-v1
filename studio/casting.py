"""AI casting proposals: API use is opt-in and approval remains human-only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from studio.series import SeriesBible, get_series, series_path, write_json


def propose_cast(root: Path, slug: str, concept: str, confirm_spend: bool) -> Path:
    """Generate an editable cast proposal. Never changes the locked canon."""
    if not confirm_spend:
        raise ValueError("AI casting uses the paid Anthropic API; pass --confirm-spend")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is required for paid cast proposals")
    bible, ledger = get_series(root, slug)
    if bible.cast_approved or ledger.last_approved_episode:
        raise ValueError("Cannot replace an approved or in-progress series cast")
    target = series_path(root, slug) / "cast_proposal.json"
    if target.exists():
        raise FileExistsError("Cast proposal exists. Review it; do not overwrite paid work")

    # Import only inside an explicitly approved paid action.
    from anthropic import Anthropic

    instructions = (root / "STORY_STANDARD.md").read_text(encoding="utf-8")
    channel = (root / "config" / "channel_bible.json").read_text(encoding="utf-8")
    response = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]).messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=6500,
        temperature=0.7,
        system=(
            "You are TaleNovi Studio's original fiction showrunner and casting director. "
            "Create independently invented stories and emotionally complex original characters. "
            "Never copy, remix or closely paraphrase any public story, copyrighted character, "
            "film or social-media script. PG-safe: no vulgarity, explicit sexual content, gore, "
            "hateful material, or exploits of real people. Output ONLY valid JSON matching "
            "the requested schema. Voice env_key must only be an environment variable name; "
            "do not invent actual voice IDs or include API credentials. "
            "Two or three recurring characters is ideal; never more than five. "
            "Each episode needs an earned payoff and an emotional progression, with a season finale."
        ),
        messages=[{"role": "user", "content": (
            f"SERIES SLUG: {slug}\nSTORY CONCEPT: {concept}\n"
            f"CURRENT WORKING BIBLE (may be replaced): {bible.model_dump_json()}\n"
            f"CHANNEL: {channel}\nEDITORIAL STANDARD: {instructions}\n"
            "Create a complete SeriesBible JSON object with fields: slug, title, premise, genre, tone, "
            "target_audience, visual_style, content_rating='PG', world_rules, season_goal, "
            "story_principles, characters, voices, season_outline. "
            "Each character needs id (lowercase snake_case), name, age_range, appearance, "
            "signature_wardrobe, temperament, core_desire, fear_or_wound, speech_style, "
            "relationships (keys are other cast IDs), reference_images=[], internal_need, "
            "flaw, emotional_triggers, personality_arc and signature_prop. "
            "Create exactly six season_outline objects with number (1..6), premise, "
            "emotional_progression, distinct_payoff, forward_question. All six plot beats must be "
            "different and connected causally. "
            "voices must map narrator and each character ID to voice profiles: env_key, "
            "model_id='eleven_multilingual_v2', stability=0.55, similarity_boost=0.75, "
            "style=0.0, speed=1.0, accent and voice_direction. "
            "Use ELEVENLABS_VOICE_ID for narrator and ELEVENLABS_{CHARACTER_ID_UPPER}_VOICE_ID "
            "for each character. Set cast_approved=false, cast_version=0 and cast_approved_at_utc=null. "
            "Your response will be reviewed by a human, not approved automatically."
        )}],
    )
    content = "".join(getattr(block, "text", "") for block in response.content).strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    draft = SeriesBible.model_validate(json.loads(content))
    if draft.slug != slug:
        raise ValueError("AI cast proposal slug does not match requested series")
    if not (2 <= len(draft.characters) <= 5):
        raise ValueError("Cast proposal must include 2–5 recurring characters")
    if sorted(beat.number for beat in draft.season_outline) != list(range(1, 7)):
        raise ValueError("Cast proposal must include exactly six different season beats")
    # Never let API output self-approve or set canon versions.
    draft = draft.model_copy(update={"cast_approved": False, "cast_version": 0, "cast_approved_at_utc": None})
    write_json(target, draft.model_dump(mode="json"))
    return target
