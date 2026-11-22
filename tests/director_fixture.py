"""A small, fully local show used by tests and `director.py demo` (placeholder art, no network)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
DEMO_ENV = {"ELEVENLABS_DEMO_A_VOICE_ID": "demo-voice-a", "ELEVENLABS_DEMO_B_VOICE_ID": "demo-voice-b"}


def _png(path: Path, label: str, colour: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (540, 960), colour)
    ImageDraw.Draw(img).text((40, 440), label, fill=(255, 255, 255))
    img.save(path)


def _w(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def demo_shots(n: int = 18, seconds: float = 5.0) -> list[dict]:
    shots = []
    lines = ["You kept the lantern lit all night.", "Someone had to, the path floods after dark.",
             "Then let me carry it tomorrow.", "Only if you promise to come back before the rain."]
    moves = ["push_in", "pan_left", "static_locked", "dolly_right", "tracking", "pull_out"]
    for i in range(n):
        speaker = None
        line = ""
        if i % 3 == 1:
            line = lines[(i // 3) % len(lines)]
            speaker = "pip" if (i // 3) % 2 == 0 else "bram"
        shots.append({
            "shot_id": f"S{i + 1:02d}", "duration_sec": seconds,
            "character_ids": ["pip", "bram"] if i % 4 == 0 else ([speaker] if speaker else ["pip"]),
            "location_id": "orchard", "camera_movement": moves[i % len(moves)],
            "framing": "close" if line else ("wide" if i % 4 == 0 else "medium"),
            "action": "Pip lifts the lantern and the leaves sway in the wind",
            "dialogue_line": line, "speaker_id": speaker,
            "voice_id": {"pip": "ELEVENLABS_DEMO_A_VOICE_ID", "bram": "ELEVENLABS_DEMO_B_VOICE_ID"}.get(speaker),
            "lip_sync_required": bool(line),
            "transition_in": "fadeblack" if i == 9 else "cut",
        })
    return shots


def build_demo_show(tmp: Path, lock: bool = True) -> Path:
    root = tmp / "repo"
    (root / "config").mkdir(parents=True)
    for name in ("studio.json", "providers.json"):
        shutil.copy2(REPO / "config" / name, root / "config" / name)
    show = root / "shows" / "demo-show"
    bible = show / "bible"
    chars = {"pip": ("Pip", "a young pear", "round green pear with a leaf on top, big amber eyes", "tiny red scarf",
                     (60, 120, 60), "ELEVENLABS_DEMO_A_VOICE_ID"),
             "bram": ("Bram", "an old apple", "wrinkled red apple, white eyebrows", "brown knitted vest",
                      (140, 40, 40), "ELEVENLABS_DEMO_B_VOICE_ID")}
    voices = {}
    for cid, (name, age, look, wardrobe, colour, key) in chars.items():
        refs = ["front.png", "three_quarter.png", "full_body.png"]
        for r in refs:
            _png(bible / "characters" / cid / "refs" / r, f"{name} {r}", colour)
        _w(bible / "characters" / cid / "character.json",
           {"character_id": cid, "name": name, "age": age, "look": look, "wardrobe": wardrobe,
            "style": "3D animated, soft clay look", "refs": refs})
        voices[cid] = {"voice_env_key": key, "model_id": "eleven_multilingual_v2"}
    _w(bible / "voices.json", voices)
    for p in ("plate_a.png", "plate_b.png"):
        _png(bible / "locations" / "orchard" / "plates" / p, f"orchard {p}", (40, 60, 90))
    _w(bible / "locations" / "orchard" / "location.json",
       {"location_id": "orchard", "name": "Moonlit orchard", "description": "rows of trees on a hill",
        "lighting": "cool moonlight with warm lantern glow", "ambience": "night", "plates": ["plate_a.png", "plate_b.png"]})
    if lock:
        from studio.director.bible import load_bible
        load_bible(show).lock(DEMO_ENV, "test")
    _w(show / "episodes" / "ep01" / "shotlist.json",
       {"show_slug": "demo-show", "episode_number": 1, "title": "The Lantern", "logline": "Pip asks to carry the lantern.",
        "music": {"source": "ai", "prompt": "gentle music box"},
        "canon_updates": ["Pip carries the lantern from now on"], "character_knowledge": {"pip": "the path floods",
        "bram": "Pip is ready"}, "next_episode_starts": "Pip's first night alone with the lantern",
        "shots": demo_shots()})
    return root
