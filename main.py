"""Local-first faceless story production starter.

Demo mode runs without credentials. Real production uses Anthropic, fal, and ElevenLabs.
Nothing in this application publishes to social platforms automatically.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from dotenv import load_dotenv
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field, ValidationError

from studio.series import (create_series, create_cast_template, approve_cast,
                           get_series, record_approval, write_json)
from studio.casting import propose_cast
from studio.story_checks import preflight
from studio.multiformat import export_all, quality_report

load_dotenv()
ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"


class Scene(BaseModel):
    narration: str = Field(min_length=15, max_length=800)
    visual_prompt: str = Field(min_length=20, max_length=1500)
    speaker_id: str = "narrator"  # Each scene uses one stable voice; dialogue mixing is a later feature.
    character_ids: list[str] = Field(default_factory=list)
    emotional_beat: str = ""
    visual_continuity_notes: str = ""


class Story(BaseModel):
    title: str = Field(min_length=5, max_length=110)
    hook: str = Field(min_length=5, max_length=200)
    summary: str = Field(min_length=20, max_length=500)
    scenes: list[Scene] = Field(min_length=3, max_length=12)
    youtube_description: str = Field(min_length=20, max_length=2000)
    tags: list[str] = Field(min_length=1, max_length=12)
    series_slug: str | None = None
    episode_number: int | None = Field(default=None, ge=1)
    character_ids: list[str] = Field(default_factory=list)
    emotional_turn: str = ""
    ending_payoff: str = ""
    unresolved_threads: list[str] = Field(default_factory=list)
    canon_updates: list[str] = Field(default_factory=list)
    originality_notes: str = "Created as new fiction; no external story text supplied."
    # v6 canon hand-off (required for story-folder episodes by `check`):
    character_knowledge: dict[str, list[str]] = Field(default_factory=dict)  # character_id -> what they know now
    next_episode_starts: str = ""        # where the next episode begins (or the finale's payoff)

    @property
    def narration(self) -> str:
        return "\n\n".join(scene.narration for scene in self.scenes)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one known local binary with safe, non-shell argument passing."""
    return subprocess.run(list(args), check=True, capture_output=True, text=True)


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Missing {name}. Install it and add it to PATH.")


def slugify(text: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (result[:55] or "episode").strip("-")


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_status(folder: Path, stage: str, **extra: object) -> None:
    """Store stage state so an interrupted episode can be retried safely."""
    path = folder / "status.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update(stage=stage, updated_utc=datetime.now(timezone.utc).isoformat(), **extra)
    save_json(path, current)


def create_folder(title: str) -> Path:
    OUTPUTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    folder = OUTPUTS / f"{stamp}-{slugify(title)}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True)
    return folder


def load_story(folder: Path) -> Story:
    return Story.model_validate_json((folder / "story.json").read_text(encoding="utf-8"))


def write_srt(folder: Path, story: Story, duration: float) -> None:
    """Create approximate scene-level captions; replace with Whisper word alignment later."""
    weights = [max(1, len(s.narration.split())) for s in story.scenes]
    total = sum(weights)
    offset = 0.0

    def stamp(seconds: float) -> str:
        ms = round(seconds * 1000)
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, milli = divmod(rem, 1000)
        return f"{h:02}:{m:02}:{s:02},{milli:03}"

    lines: list[str] = []
    for index, scene in enumerate(story.scenes, 1):
        segment = duration * weights[index - 1] / total
        # Scene captions in this starter are rough timing markers, not publish-ready subtitles.
        text = scene.narration[:170].replace("\n", " ").strip()
        lines.extend([str(index), f"{stamp(offset)} --> {stamp(offset + segment)}", text, ""])
        offset += segment
    (folder / "draft_scene_captions.srt").write_text("\n".join(lines), encoding="utf-8")


def audio_duration(path: Path) -> float:
    require_binary("ffprobe")
    result = run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path))
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise ValueError("Audio duration must be positive")
    return duration


def script_hash(folder: Path) -> str:
    """Invalidate script approval whenever the underlying story JSON changes."""
    return hashlib.sha256((folder / "story.json").read_bytes()).hexdigest()


def episode_checks(folder: Path, story: Story, root: Path = ROOT) -> dict:
    """Offline preflight + storyboard + runtime estimate (+ v6 contract for story-folder episodes)."""
    bible_data, canon = get_series(root, story.series_slug)
    result = preflight(story, bible_data, canon, root=root)
    from studio.episode_production import GAP_S, TAIL_S, load_studio_config
    from studio.stories import (duration_limits, estimate_runtime, is_story_episode,
                                story_contract_episode_errors, storyboard_checks)
    strict = is_story_episode(folder)
    limits = duration_limits(root)
    estimate = estimate_runtime(story, GAP_S, TAIL_S)
    result["runtime_estimate_seconds"] = round(estimate, 1)
    result["runtime_limits"] = limits
    board_path = folder / "storyboard.json"
    if board_path.is_file():
        from studio.episode_production import scene_holds
        estimate += sum(scene_holds(json.loads(board_path.read_text(encoding="utf-8")), len(story.scenes)))
        result["runtime_estimate_seconds"] = round(estimate, 1)
        motion_budget = load_studio_config(root).get("episode_caps", {}).get("motion_seconds", 30)
        errs, warns = storyboard_checks(story, json.loads(board_path.read_text(encoding="utf-8")),
                                        bible_data, strict=strict, motion_seconds=int(motion_budget),
                                        runtime_estimate=estimate, limits=limits)
        result["errors"] += errs
        result["warnings"] += warns
    elif strict:
        result["errors"].append("storyboard.json is missing")
    if strict:
        result["errors"] += story_contract_episode_errors(story)
    result["passed"] = not result["errors"]
    return result


def approve_script(folder: Path, confirm_reviewed: bool, reviewer: str = "human", root: Path = ROOT) -> None:
    """Recorded script review (human, or Claude Code in autonomous mode) before paid media."""
    if not confirm_reviewed:
        raise ValueError("Personally read and approve the entire script, then use --confirm-reviewed")
    story = load_story(folder)
    if story.series_slug:
        result = episode_checks(folder, story, root)
        write_json(folder / "story_preflight.json", result)
        if not result["passed"]:
            raise ValueError(f"Script preflight failed: {result['errors']}")
    save_status(folder, "SCRIPT_APPROVED", script_approved=True, script_reviewer=reviewer,
                approved_story_sha256=script_hash(folder), publication_approved=False)
    print("Script approved for optional paid generation. Nothing was purchased.")


def plan(idea: str, series_slug: str | None = None, confirm_spend: bool = False) -> Path:
    """Use the paid Anthropic API after explicit permission to draft a story."""
    if not confirm_spend:
        raise ValueError("Story planning calls the paid Anthropic API; use --confirm-spend")
    from anthropic import Anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("Set ANTHROPIC_API_KEY in your .env for the 'plan' command")
    system_prompt = (ROOT / "prompts" / "story_system.txt").read_text(encoding="utf-8")
    bible = (ROOT / "config" / "channel_bible.json").read_text(encoding="utf-8")
    # Load locked canon and the most recently approved episodes before writing the next part.
    series_context = ""
    expected_number = None
    if series_slug:
        bible_data, canon = get_series(ROOT, series_slug)
        if not bible_data.cast_approved:
            raise ValueError("First approve the proposed cast with cast-approve")
        expected_number = canon.last_approved_episode + 1
        recent = [entry.model_dump(exclude={"story_text"}) for entry in canon.approved_episodes[-6:]]
        series_context = (
            "SERIES BIBLE (locked facts; do not retcon):\n"
            + bible_data.model_dump_json(indent=2)
            + "\nAPPROVED CANON SUMMARY (latest six):\n"
            + json.dumps(recent, indent=2)
            + f"\nThis must be episode {expected_number} in series {series_slug}. "
            "Use registered character IDs, speaker IDs, personalities, wardrobes and world rules. "
            "Give a satisfying ending payoff AND a meaningful forward question. "
            "Use no copied or lightly paraphrased existing stories. "
            "Your story must be meaningfully different from previous episodes.\n"
        )
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=3500,
        temperature=0.7,
        system=system_prompt,
        messages=[{"role": "user", "content": (
            f"Create an original, emotionally engaging fictional episode based on: {idea}\n\n"
            f"Channel bible:\n{bible}\n\n"
            + series_context
            + "Return ONLY a valid JSON object: title, hook, summary, scenes (6 to 8 "
              "objects with narration, visual_prompt, speaker_id, character_ids, emotional_beat "
              "and visual_continuity_notes), youtube_description, tags, series_slug, "
              "episode_number, character_ids, emotional_turn, ending_payoff, unresolved_threads, "
              "canon_updates, originality_notes. For a non-series episode set series_slug and "
              "episode_number to null and character_ids to []. Target ~150–190 narrated words. "
              "No vulgarity, sexual content, gore, or copied plots. Every episode must deliver "
              "its own coherent resolution, with an earned forward hook only if appropriate."
        )}],
    )
    content = "".join(getattr(block, "text", "") for block in response.content).strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    story = Story.model_validate(json.loads(content))
    if series_slug:
        if story.series_slug != series_slug or story.episode_number != expected_number:
            raise ValueError("LLM did not respect series or episode number; refusing plan")
        check = preflight(story, bible_data, canon, root=ROOT)
        if not check["passed"]:
            raise ValueError(f"Series preflight failed: {check['errors']}")
    folder = create_folder(story.title)
    (folder / "story.json").write_text(story.model_dump_json(indent=2), encoding="utf-8")
    (folder / "script.txt").write_text(story.narration, encoding="utf-8")
    save_json(folder / "metadata.json", {"title": story.title, "description": story.youtube_description,
                                          "tags": story.tags, "ai_generated": True})
    if series_slug:
        write_json(folder / "story_preflight.json", check)
    save_status(folder, "PLANNED", idea=idea, series_slug=series_slug,
                episode_number=expected_number, publication_approved=False)
    print(f"PLAN CREATED: {folder}\nReview story.json before paid generation.")
    return folder


def generate_voice_segments(folder: Path, story: Story, voices: dict) -> tuple[Path, list[float]]:
    """Generate stable per-scene voices and persist timing; requires paid TTS calls."""
    import requests

    voice_dir = folder / "voice_segments"
    voice_dir.mkdir(exist_ok=True)
    segments: list[Path] = []
    manifest: list[dict] = []
    for index, scene in enumerate(story.scenes, start=1):
        profile = voices.get(scene.speaker_id)
        if profile is None:
            raise ValueError(f"No voice profile for scene speaker {scene.speaker_id!r}")
        voice_id = os.getenv(profile.env_key)
        if not voice_id:
            raise RuntimeError(f"Set {profile.env_key} in .env for speaker {scene.speaker_id}")
        target = voice_dir / f"scene_{index:02}.mp3"
        if not target.exists():
            response = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"],
                         "Content-Type": "application/json", "Accept": "audio/mpeg"},
                json={"text": scene.narration, "model_id": profile.model_id,
                      "voice_settings": {"stability": profile.stability,
                                         "similarity_boost": profile.similarity_boost,
                                         "style": profile.style, "speed": profile.speed}},
                timeout=180,
            )
            response.raise_for_status()
            if not response.content or len(response.content) > 30_000_000:
                raise RuntimeError("Unexpected empty/oversized speech payload")
            target.write_bytes(response.content)
        seconds = audio_duration(target)
        manifest.append({"scene": index, "speaker_id": scene.speaker_id,
                         "voice_env_key": profile.env_key, "model_id": profile.model_id,
                         "duration_seconds": round(seconds, 3)})
        segments.append(target)
    write_json(folder / "voice_manifest.json", {"segments": manifest})
    # Concatenate already generated segments without calling the TTS API again.
    concat = voice_dir / "concat.txt"
    concat.write_text("".join(f"file '{p.name}'\n" for p in segments), encoding="utf-8")
    final_voice = folder / "voice.mp3"
    run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
        "-safe", "0", "-i", str(concat), "-c:a", "libmp3lame", "-q:a", "3",
        str(final_voice))
    return final_voice, [item["duration_seconds"] for item in manifest]


def produce(folder: Path, confirm_spend: bool) -> Path:
    """Run paid media generation only with explicit opt-in."""
    if not confirm_spend:
        raise ValueError("Paid generation requires --confirm-spend after reviewing story.json")
    status_path = folder / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    if not status.get("script_approved") or status.get("approved_story_sha256") != script_hash(folder):
        raise ValueError("Script not approved, or changed after approval. Run approve-script --confirm-reviewed")
    try:
        import fal_client
        import requests
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt first") from exc

    for name in ("FAL_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
        if not os.getenv(name):
            raise RuntimeError(f"Set {name} in your .env before producing")

    story = load_story(folder)
    series_bible = None
    if story.series_slug:
        series_bible, ledger = get_series(ROOT, story.series_slug)
        check = preflight(story, series_bible, ledger, root=ROOT)
        write_json(folder / "story_preflight.json", check)
        if not check["passed"]:
            raise ValueError(f"Series preflight failed: {check['errors']}")
    images_dir = folder / "images"
    images_dir.mkdir(exist_ok=True)
    bible = json.loads((ROOT / "config" / "channel_bible.json").read_text(encoding="utf-8"))
    model = os.getenv("FAL_IMAGE_MODEL", "fal-ai/flux-2/flash")
    style = bible.get("visual_style", "cinematic original science-fiction art")

    save_status(folder, "GENERATING_MEDIA", publication_approved=False)
    for index, scene in enumerate(story.scenes, 1):
        destination = images_dir / f"scene_{index:02}.png"
        if destination.exists():
            continue  # Resume after interruption without regenerating paid images.
        # Anchor each image to the permanent character profile, not just an ad-hoc prompt.
        character_anchor = ""
        if series_bible:
            cast = {c.id: c for c in series_bible.characters}
            character_anchor = " ".join(
                f"PERSON {cast[cid].name}: {cast[cid].appearance}; "
                f"WARDROBE: {cast[cid].signature_wardrobe}."
                for cid in scene.character_ids
            )
        result = fal_client.subscribe(model, arguments={"image_size": "portrait_16_9", "prompt": (
            f"{style}. {series_bible.visual_style if series_bible else ''}. "
            f"{character_anchor} {scene.visual_continuity_notes}. {scene.visual_prompt}. "
            "Portrait 9:16 composition, frame all characters naturally for a mobile screen. "
            "High detail, no text, no logos, "
            "original characters, coherent appearance and wardrobe."
        )})
        url = result["images"][0]["url"]
        image_response = requests.get(url, timeout=120)
        image_response.raise_for_status()
        if len(image_response.content) > 25_000_000:
            raise RuntimeError("Image file over 25 MB; refusing unexpected response")
        # Normalize any provider image format to PNG for FFmpeg input reliability.
        from io import BytesIO
        with Image.open(BytesIO(image_response.content)) as image:
            image.convert("RGB").save(destination, format="PNG")
        print(f"Saved image {index}/{len(story.scenes)}")

    # Series episodes can use persistent voice settings per scene; ordinary videos
    # retain the narrator-only mode for backwards compatibility.
    audio_path = folder / "voice.mp3"
    scene_durations = None
    if series_bible:
        audio_path, scene_durations = generate_voice_segments(folder, story, series_bible.voices)
    elif not audio_path.exists():
        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{os.environ['ELEVENLABS_VOICE_ID']}",
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"],
                     "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"text": story.narration,
                  "model_id": os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")},
            timeout=180,
        )
        response.raise_for_status()
        audio_path.write_bytes(response.content)

    exports = export_all(folder, len(story.scenes), audio_path,
                         [len(scene.narration.split()) for scene in story.scenes],
                         scene_durations=scene_durations)
    write_srt(folder, story, audio_duration(audio_path))
    save_json(folder / "qc.json", quality_report(exports))
    save_json(folder / "exports.json", exports)
    final_path = Path(exports["master"])
    save_status(folder, "NEEDS_HUMAN_REVIEW", publication_approved=False)
    print(f"FINISHED VIDEO FOR REVIEW: {final_path}")
    return final_path


def demo() -> Path:
    """Create a tiny offline sample proving FFmpeg and local saving work."""
    from PIL import ImageFont

    require_binary("ffmpeg")
    scenes = [
        Scene(narration="At midnight, an impossible message appeared.",
              visual_prompt="A dark city with a glowing blue tower and midnight rain."),
        Scene(narration="The sender was someone who had vanished years ago.",
              visual_prompt="A mysterious phone under cold light, no readable text."),
        Scene(narration="Then the message appeared again, from tomorrow.",
              visual_prompt="A silhouette beside a doorway filled with stars."),
    ]
    story = Story(title="Offline Demo - Mystery Pilot", hook="A message from tomorrow.",
                  summary="A synthetic offline test for the local rendering pipeline.",
                  scenes=scenes, youtube_description="Offline diagnostic demonstration only; not for publication.",
                  tags=["demo", "mystery"])
    folder = create_folder(story.title)
    (folder / "story.json").write_text(story.model_dump_json(indent=2), encoding="utf-8")
    (folder / "script.txt").write_text(story.narration, encoding="utf-8")
    img_dir = folder / "images"
    img_dir.mkdir()
    colors = [(16, 28, 64), (37, 24, 56), (15, 51, 61)]
    for i, (scene, color) in enumerate(zip(story.scenes, colors), 1):
        image = Image.new("RGB", (720, 1280), color)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((140, 340, 580, 930), radius=35, outline=(180, 210, 245), width=7)
        draw.text((245, 560), f"SCENE {i}", fill=(245, 245, 255))
        draw.text((172, 650), "OFFLINE RENDER TEST", fill=(245, 245, 255))
        image.save(img_dir / f"scene_{i:02}.png")
    audio_path = folder / "voice.mp3"
    run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "12",
        "-q:a", "5", str(audio_path))
    exports = export_all(folder, len(story.scenes), audio_path,
                         [len(scene.narration.split()) for scene in story.scenes], preview=True)
    output = Path(exports["master"])
    save_json(folder / "qc.json", quality_report(exports))
    save_json(folder / "exports.json", exports)
    save_status(folder, "DEMO_ONLY", publication_approved=False)
    print(f"OFFLINE DEMO CREATED: {output}\nSilent narration is expected in demo mode.")
    return output


def approve(folder: Path, confirm_reviewed: bool = False, reviewer: str = "human", root: Path = ROOT) -> None:
    """Finished-video approval: records canon exactly once; NEVER posts anywhere.

    Story-folder episodes (series/<slug>/episodes/NN) also need a passing QC report and Claude's
    editorial_review.md, then get release.json (checksum, QC, provenance, jobs, costs) and a cleanup of
    the manifest-listed scratch for this run. Re-running finishes any step an interruption skipped."""
    from studio.stories import cleanup_episode, is_story_episode, write_release
    if not confirm_reviewed:
        raise ValueError("Watch the completed video and use --confirm-reviewed to approve release")
    if not (folder / "master_9x16.mp4").exists():
        raise FileNotFoundError("No video exists for review")
    status_path = folder / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    story = load_story(folder)
    story_layout = is_story_episode(folder)
    if status.get("publication_approved"):
        if not story_layout:
            print("Already approved; no changes made.")
            return
        print("Already approved; canon unchanged. Completing release record and cleanup if needed.")
    else:
        if not status.get("script_approved") or status.get("approved_story_sha256") != script_hash(folder):
            raise ValueError("The story lacks an approved, unchanged script")
        if not (folder / "qc.json").is_file():
            raise FileNotFoundError("Technical video QA report is missing")
        if story_layout:
            report = folder / "reports" / "quality_report.json"
            if not report.is_file() or not json.loads(report.read_text(encoding="utf-8")).get("passed"):
                raise ValueError("QC has not passed (reports/quality_report.json); fix and re-render first")
            if json.loads(report.read_text(encoding="utf-8")).get("dry_run"):
                raise ValueError("This master is a dry-run placeholder; only a real render can be approved")
            if not (folder / "reports" / "editorial_review.md").is_file():
                raise FileNotFoundError("reports/editorial_review.md is missing (Claude's release review)")
        if story.series_slug:
            series_bible, ledger = get_series(root, story.series_slug)
            check = preflight(story, series_bible, ledger, root=root)
            write_json(folder / "story_preflight.json", check)
            if not check["passed"]:
                raise ValueError(f"Series preflight failed: {check['errors']}")
            record_approval(root, story)
        save_status(folder, "APPROVED_FOR_MANUAL_UPLOAD", publication_approved=True, release_reviewer=reviewer)
        print("Marked approved; canon recorded for series episodes. No upload performed.")
    if story_layout:
        if not (folder / "release.json").is_file():
            write_release(root, folder, story, reviewer)
        summary = cleanup_episode(root, folder)
        print(f"release.json written; scratch removed: {summary['removed']} item(s), "
              f"{summary['reclaimed_bytes'] / 1e6:.1f} MB reclaimed, {summary['retained_bytes'] / 1e6:.1f} MB kept.")


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-first AI Story Coworker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Render one offline vertical sample without API calls")
    reexport = commands.add_parser("export", help="Render one vertical master from existing media, no API charges")
    reexport.add_argument("--episode", required=True)
    reexport.add_argument("--preview", action="store_true", help="Use small offline test resolutions")
    planning = commands.add_parser("plan", help="Generate a story plan with Anthropic API")
    planning.add_argument("--idea", required=True)
    planning.add_argument("--series", help="Optional existing series slug, e.g. the-last-signal")
    planning.add_argument("--confirm-spend", action="store_true")
    creation = commands.add_parser("series-init", help="Create a persistent series bible and canon")
    creation.add_argument("--slug", required=True)
    cast_template = commands.add_parser("cast-template", help="Create an editable offline cast proposal")
    cast_template.add_argument("--series", required=True)
    cast_idea = commands.add_parser("cast-propose", help="AI-propose main characters and six-episode season (paid)")
    cast_idea.add_argument("--series", required=True)
    cast_idea.add_argument("--concept", required=True)
    cast_idea.add_argument("--confirm-spend", action="store_true")
    cast_ok = commands.add_parser("cast-approve", help="Lock reviewed cast, voices and story world")
    cast_ok.add_argument("--series", required=True)
    cast_ok.add_argument("--confirm-reviewed", action="store_true")
    cast_ok.add_argument("--reviewer", default="human",
                        help='Who reviewed it, e.g. "claude-code (autonomous)"; recorded for audit')
    script_ok = commands.add_parser("approve-script", help="Human approval before paid media generation")
    script_ok.add_argument("--episode", required=True)
    script_ok.add_argument("--confirm-reviewed", action="store_true")
    script_ok.add_argument("--reviewer", default="human",
                        help='Who reviewed it, e.g. "claude-code (autonomous)"; recorded for audit')
    check_cmd = commands.add_parser("check", help="Check a series episode offline before spending")
    check_cmd.add_argument("--episode", required=True)
    production = commands.add_parser("produce", help="Produce full video using paid media APIs")
    production.add_argument("--episode", required=True)
    production.add_argument("--confirm-spend", action="store_true")
    episode_cmd = commands.add_parser(
        "produce-episode",
        help="Paid: 9:16 series episode with motion clips, camera moves, captions and hard caps")
    episode_cmd.add_argument("--episode", required=True)
    episode_cmd.add_argument("--confirm-spend", action="store_true")
    episode_cmd.add_argument("--max-fal-usd", type=float, help="Hard fal.ai cap for this episode (USD)")
    episode_cmd.add_argument("--max-voice-credits", type=int, help="Hard ElevenLabs credit cap for this episode")
    episode_cmd.add_argument("--motion-seconds", type=int, default=None,
                             help="Total Wan image-to-video seconds across storyboard motion shots (0 = none)")
    episode_cmd.add_argument("--dry-run", action="store_true",
                             help="Free rehearsal in <episode>__dryrun with placeholder media at 540x960")
    episode_cmd.add_argument("--legacy-slideshow", action="store_true",
                             help="RETIRED pipeline (stills + camera moves). Paid runs need this flag and the "
                                  "owner's explicit request; new work uses director.py")
    commands.add_parser("voices", help="List the ElevenLabs voice roster from .env (free metadata lookup)")
    approval = commands.add_parser("approve", help="Mark a reviewed video ready for manual upload")
    approval.add_argument("--episode", required=True)
    approval.add_argument("--confirm-reviewed", action="store_true")
    approval.add_argument("--reviewer", default="human",
                        help='Who reviewed it, e.g. "claude-code (autonomous)"; recorded for audit')
    start_cmd = commands.add_parser("story-start", help="Start a new 5-6 episode story in series/<slug>/ (free)")
    start_cmd.add_argument("--title", required=True)
    start_cmd.add_argument("--episodes", type=int, choices=(5, 6), help="Planned episodes (default 5)")
    start_cmd.add_argument("--slug", help="Optional explicit slug; refused if it already exists")
    cont_cmd = commands.add_parser("story-continue",
                                   help="Resume the unfinished episode or allocate the next one (free)")
    cont_cmd.add_argument("--slug", required=True)
    stat_cmd = commands.add_parser("story-status", help="Completed/next episode, open question, folder (free)")
    stat_cmd.add_argument("--slug", required=True)
    clean_cmd = commands.add_parser("cleanup", help="Remove manifest-listed scratch of an approved episode")
    clean_cmd.add_argument("--episode", required=True)
    sheet_cmd = commands.add_parser("review-sheet", help="Contact sheets of stills and master frames (scratch)")
    sheet_cmd.add_argument("--episode", required=True)
    sheet_cmd.add_argument("--frames", type=int, default=8)
    reject_cmd = commands.add_parser("reject-take",
                                     help="Set aside one shot's still/clip so the next run regenerates it")
    reject_cmd.add_argument("--episode", required=True)
    reject_cmd.add_argument("--shot", required=True)
    from studio.cli_autopilot import AUTOPILOT_COMMANDS, dispatch as autopilot_dispatch, register
    register(commands)
    args = parser.parse_args()
    try:
        if args.command in AUTOPILOT_COMMANDS:
            autopilot_dispatch(ROOT, args)
        elif args.command == "demo":
            demo()
        elif args.command == "export":
            folder = Path(args.episode).expanduser().resolve()
            story = load_story(folder)
            narration = folder / "voice.mp3"
            scene_manifest = folder / "voice_manifest.json"
            scene_durations = None
            if scene_manifest.exists():
                manifest = json.loads(scene_manifest.read_text(encoding="utf-8"))
                scene_durations = [float(item["duration_seconds"]) for item in manifest["segments"]]
            exports = export_all(folder, len(story.scenes), narration,
                                 [len(scene.narration.split()) for scene in story.scenes],
                                 scene_durations=scene_durations, preview=args.preview)
            save_json(folder / "exports.json", exports)
            save_json(folder / "qc.json", quality_report(exports))
            print(json.dumps(exports, indent=2))
        elif args.command == "plan":
            plan(args.idea, series_slug=args.series, confirm_spend=args.confirm_spend)
        elif args.command == "series-init":
            print(f"NEW SERIES: {create_series(ROOT, args.slug)}")
            print("Use cast-propose (paid) or cast-template (offline), then cast-approve.")
        elif args.command == "cast-template":
            print(f"EDITABLE CAST PROPOSAL: {create_cast_template(ROOT, args.series)}")
        elif args.command == "cast-propose":
            print(f"AI CAST PROPOSAL: {propose_cast(ROOT, args.series, args.concept, args.confirm_spend)}")
            print("Review and edit proposal; it is not yet canon or an approved cast.")
        elif args.command == "cast-approve":
            if not args.confirm_reviewed:
                raise ValueError("Read cast_proposal.json and use --confirm-reviewed")
            lock = approve_cast(ROOT, args.series)
            data = json.loads(lock.read_text(encoding="utf-8"))
            data["reviewer"] = args.reviewer
            write_json(lock, data)
            print(f"CAST LOCKED: {lock}")
        elif args.command == "approve-script":
            approve_script(Path(args.episode).expanduser().resolve(), args.confirm_reviewed, args.reviewer)
        elif args.command == "check":
            folder = Path(args.episode).expanduser().resolve()
            story = load_story(folder)
            if not story.series_slug:
                raise ValueError("This check command is for series episodes")
            result = episode_checks(folder, story)
            write_json(folder / "story_preflight.json", result)
            print(json.dumps(result, indent=2))
            if not result["passed"]:
                sys.exit(2)
        elif args.command == "produce":
            produce(Path(args.episode).expanduser().resolve(), args.confirm_spend)
        elif args.command == "voices":
            from dotenv import dotenv_values
            from studio.voices import describe_roster, elevenlabs_fetcher, write_roster
            env = {**dotenv_values(ROOT / ".env"), **{k: v for k, v in os.environ.items() if k.startswith("ELEVENLABS_")}}
            if not env.get("ELEVENLABS_API_KEY"):
                raise RuntimeError("Set ELEVENLABS_API_KEY in .env")
            rows = describe_roster(env, elevenlabs_fetcher(env["ELEVENLABS_API_KEY"]))
            for r in rows:
                state = "OK " if r["found_in_account"] else "MISSING"
                alias = f" (same voice as {r['same_voice_as']})" if r["same_voice_as"] else ""
                print(f"{state} {r['role']:<18} {r['voice_name'] or '-'} | {r['gender'] or ''} {r['age'] or ''} "
                      f"{r['accent'] or ''} | {r['description'] or ''}{alias}")
            print(f"Roster saved (names/labels only): {write_roster(ROOT, rows)}")
        elif args.command == "produce-episode":
            if not args.dry_run and not args.legacy_slideshow:
                print("produce-episode is the retired still-image pipeline. New episodes use director.py "
                      "(motion shots + lip sync). Finishing a legacy story needs --legacy-slideshow and the "
                      "owner's explicit request.")
                sys.exit(2)
            from studio.episode_production import Production, SpendCapExceeded, dry_run_folder
            folder = Path(args.episode).expanduser().resolve()
            from studio.episode_production import load_studio_config
            studio = load_studio_config(ROOT)
            caps = studio.get("episode_caps", {})
            fal_cap = args.max_fal_usd if args.max_fal_usd is not None else caps.get("max_fal_usd")
            voice_cap = (args.max_voice_credits if args.max_voice_credits is not None
                         else caps.get("max_voice_credits"))
            motion = args.motion_seconds if args.motion_seconds is not None else caps.get("motion_seconds", 30)
            month_cap = studio.get("monthly_caps", {}).get("max_total_usd")
            music_default = studio.get("music", {}).get("default_source", "none")
            if args.dry_run:
                job = Production(ROOT, dry_run_folder(folder), max_fal_usd=fal_cap or 6.0,
                                 max_voice_credits=voice_cap or 10_000, motion_seconds=motion,
                                 width=540, height=960, dry_run=True, music_default=music_default)
            else:
                if fal_cap is None or voice_cap is None:
                    raise ValueError("Paid runs need caps: pass --max-fal-usd/--max-voice-credits or set "
                                     "episode_caps in config/studio.json")
                job = Production(ROOT, folder, max_fal_usd=float(fal_cap), max_voice_credits=int(voice_cap),
                                 motion_seconds=int(motion), monthly_cap_usd=month_cap,
                                 music_default=music_default)
            try:
                master = job.run(args.confirm_spend)
            except SpendCapExceeded as exc:
                raise RuntimeError(f"STOPPED BEFORE EXCEEDING BUDGET: {exc}") from exc
            print(f"EPISODE RENDERED: {master}")
        elif args.command == "approve":
            approve(Path(args.episode).expanduser().resolve(), args.confirm_reviewed, args.reviewer)
        elif args.command == "story-start":
            from studio.stories import start_story
            print_json(start_story(ROOT, args.title, args.episodes, args.slug))
        elif args.command == "story-continue":
            from studio.stories import continue_story
            print_json(continue_story(ROOT, args.slug))
        elif args.command == "story-status":
            from studio.stories import story_status
            print_json(story_status(ROOT, args.slug))
        elif args.command == "cleanup":
            from studio.stories import cleanup_episode
            print_json(cleanup_episode(ROOT, Path(args.episode).expanduser().resolve()))
        elif args.command == "review-sheet":
            from studio.stories import review_sheets
            print_json(review_sheets(Path(args.episode).expanduser().resolve(), args.frames))
        elif args.command == "reject-take":
            from studio.stories import reject_take
            print_json(reject_take(Path(args.episode).expanduser().resolve(), args.shot))
    except (RuntimeError, ValueError, FileNotFoundError, FileExistsError, ValidationError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
