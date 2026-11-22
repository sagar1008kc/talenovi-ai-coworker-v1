"""Background score: an AI-composed ElevenLabs Music track per episode, or an owner-licensed library track.

ElevenLabs Music (verified 2026-11-20, https://elevenlabs.io/docs/api-reference/music/compose):
POST /v1/music, JSON {prompt, music_length_ms 3000-600000, model_id, force_instrumental}, returns audio bytes.
Price on the pricing page: 900 credits per minute of generated music. Commercial use is permitted from the Starter
plan up (film, TV, radio and games excluded); verify the live terms before changing plans.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from studio.budget import CostLedger

MUSIC_URL = "https://api.elevenlabs.io/v1/music"
MUSIC_MODEL = "music_v2_5"          # music_v1 is deprecated (removal 2027-01-31); set the model explicitly
CREDITS_PER_MINUTE = 900
DEFAULT_STYLE = ("instrumental cinematic underscore for an intimate drama, sparse, emotional, unobtrusive, leaves room "
                 "for dialogue, no vocals, no singing, no lyrics, no drums")


def music_credits(seconds: float) -> int:
    return math.ceil(seconds / 60 * CREDITS_PER_MINUTE)


def build_prompt(music: dict, scene_starts: list[float], total: float) -> str:
    """Turn the storyboard's music cues (by scene number) into a timestamped arc the model can follow."""
    style = music.get("style") or DEFAULT_STYLE
    cues = []
    for cue in music.get("cues", []):
        start = scene_starts[cue["from_scene"] - 1]
        end = scene_starts[cue["to_scene"]] if cue["to_scene"] < len(scene_starts) else total
        cues.append(f"{_mmss(start)}-{_mmss(end)}: {cue['mood']}")
    arc = " ".join(cues)
    prompt = f"{style}. Total length {_mmss(total)}. Arc: {arc}. Ends with a soft, held resolution."
    return prompt[:4000]


def _mmss(seconds: float) -> str:
    s = max(0, round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def compose_ai_score(*, api_key: str, prompt: str, seconds: float, dest: Path, ledger: CostLedger,
                     guard: Any) -> dict:
    """Generate (once) and cache an instrumental score. `guard(credits, item)` enforces the credit cap first."""
    meta_path = dest.with_suffix(".json")
    if dest.is_file() and meta_path.is_file():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    import requests

    credits = music_credits(seconds)
    guard(credits, "music score")
    entry = ledger.reserve("elevenlabs", MUSIC_MODEL, "music score", estimated_usd=0.0, credits=credits)
    try:
        resp = requests.post(MUSIC_URL, params={"output_format": "mp3_48000_192"},
                             headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                             json={"prompt": prompt, "music_length_ms": int(round(seconds * 1000)),
                                   "model_id": MUSIC_MODEL, "force_instrumental": True},
                             timeout=(15, 600))
        if resp.status_code != 200 or len(resp.content) < 10_000:
            raise RuntimeError(f"ElevenLabs music HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as exc:
        ledger.settle(entry, "failed_unbilled", error=str(exc))
        raise
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    meta = {"source": "ai", "provider": "elevenlabs", "model": MUSIC_MODEL, "prompt": prompt,
            "seconds": round(seconds, 2), "credits_estimated": credits,
            "song_id": resp.headers.get("song-id") or resp.headers.get("x-song-id"),
            "license": "ElevenLabs Music generated on the owner's paid plan; commercial online use per "
                       "https://elevenlabs.io/music-terms"}
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    ledger.settle(entry, "ok")
    return meta


def library_track(root: Path, file: str) -> tuple[Path, dict]:
    manifest = json.loads((root / "assets" / "music" / "provenance.json").read_text(encoding="utf-8"))
    track = next((t for t in manifest["tracks"] if t["file"] == file), None)
    if track is None or not track.get("commercial_use_confirmed"):
        raise ValueError(f"Music track {file!r} is not listed with commercial_use_confirmed=true")
    path = root / "assets" / "music" / file
    if not path.is_file():
        raise FileNotFoundError(path)
    return path, {"source": "library", **track}
