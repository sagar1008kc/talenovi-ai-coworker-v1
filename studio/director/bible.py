"""Locked show bible: character / location registries, the voice map and their SHA-256 lock.

Once `bible.lock.json` exists, any change to a locked character, location, reference image, voice
entry or the resolved ElevenLabs voice ID fails `verify()`. New characters/locations may be added and
locked later (`lock(..., extend=True)`), but locked entries never change silently.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from studio.director.schema import Character, Location, VoiceEntry

LOCK_NAME = "bible.lock.json"


class BibleError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Bible:
    root: Path                                   # shows/<slug>/bible
    characters: dict[str, Character]
    locations: dict[str, Location]
    voices: dict[str, VoiceEntry]

    # ------------------------------------------------------------ paths
    def character_dir(self, cid: str) -> Path:
        return self.root / "characters" / cid

    def location_dir(self, lid: str) -> Path:
        return self.root / "locations" / lid

    def character_refs(self, cid: str) -> list[Path]:
        c = self.characters[cid]
        return [self.character_dir(cid) / "refs" / r for r in c.refs]

    def location_plates(self, lid: str) -> list[Path]:
        loc = self.locations[lid]
        return [self.location_dir(lid) / "plates" / p for p in loc.plates]

    # ------------------------------------------------------------ fingerprint
    def entries(self, env: dict | None = None) -> dict[str, str]:
        """Every lockable item -> SHA-256. Voice IDs are hashed, never stored."""
        out: dict[str, str] = {}
        for cid, c in sorted(self.characters.items()):
            out[f"character:{cid}"] = sha256_text(c.model_dump_json())
            for ref in self.character_refs(cid):
                out[f"character:{cid}:ref:{ref.name}"] = sha256_file(ref) if ref.is_file() else "MISSING"
        for lid, loc in sorted(self.locations.items()):
            out[f"location:{lid}"] = sha256_text(loc.model_dump_json())
            for plate in self.location_plates(lid):
                out[f"location:{lid}:plate:{plate.name}"] = sha256_file(plate) if plate.is_file() else "MISSING"
        for cid, v in sorted(self.voices.items()):
            out[f"voice:{cid}"] = sha256_text(v.model_dump_json())
            if env is not None:
                vid = env.get(v.voice_env_key, "")
                out[f"voice:{cid}:id"] = sha256_text(vid) if vid else "UNSET"
        return out

    def validate(self) -> list[str]:
        errors = []
        for cid in self.characters:
            if cid not in self.voices:
                errors.append(f"character {cid} has no entry in bible/voices.json")
            for ref in self.character_refs(cid):
                if not ref.is_file():
                    errors.append(f"missing reference image {ref}")
        for cid in self.voices:
            if cid not in self.characters:
                errors.append(f"voices.json maps unknown character {cid}")
        keys = [v.voice_env_key for v in self.voices.values()]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            errors.append(f"two characters share a voice role: {sorted(dupes)}")
        for lid in self.locations:
            for plate in self.location_plates(lid):
                if not plate.is_file():
                    errors.append(f"missing location plate {plate}")
        return errors

    # ------------------------------------------------------------ lock
    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_NAME

    def lock(self, env: dict, reviewer: str, extend: bool = False) -> dict:
        errors = self.validate()
        unset = [cid for cid, v in self.voices.items() if not env.get(v.voice_env_key)]
        if unset:
            errors.append(f"voice IDs not set in .env for: {unset}")
        if errors:
            raise BibleError("; ".join(errors))
        current = self.entries(env)
        if self.lock_path.exists():
            if not extend:
                raise BibleError("bible is already locked; use extend=True to add NEW characters/locations only")
            old = json.loads(self.lock_path.read_text(encoding="utf-8"))["entries"]
            changed = [k for k, v in old.items() if current.get(k) != v]
            if changed:
                raise BibleError(f"locked entries changed (a recast needs a new show version): {changed}")
        from studio.models import utc_now
        data = {"locked_by": reviewer, "locked_utc": utc_now(), "entries": current}
        self.lock_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return data

    def verify(self, env: dict | None = None) -> list[str]:
        """Problems with the lock ([] = locked and intact). env=None skips the voice-ID check (dry runs)."""
        if not self.lock_path.exists():
            return ["bible is not locked (run director.py bible-lock)"]
        locked = json.loads(self.lock_path.read_text(encoding="utf-8"))["entries"]
        current = self.entries(env)
        problems = []
        for key, value in locked.items():
            if key.endswith(":id") and env is None:
                continue
            if current.get(key) != value:
                problems.append(f"{key} changed since lock" if key in current else f"{key} removed since lock")
        return problems

    def is_locked(self, cid_or_lid: str) -> bool:
        if not self.lock_path.exists():
            return False
        locked = json.loads(self.lock_path.read_text(encoding="utf-8"))["entries"]
        return f"character:{cid_or_lid}" in locked or f"location:{cid_or_lid}" in locked


def load_bible(show_dir: Path) -> Bible:
    root = show_dir / "bible"
    if not root.is_dir():
        raise BibleError(f"{root} does not exist (run director.py show-init)")
    chars, locs = {}, {}
    for f in sorted((root / "characters").glob("*/character.json")):
        c = Character.model_validate_json(f.read_text(encoding="utf-8"))
        if c.character_id != f.parent.name:
            raise BibleError(f"{f}: character_id must match its folder name")
        chars[c.character_id] = c
    for f in sorted((root / "locations").glob("*/location.json")):
        loc = Location.model_validate_json(f.read_text(encoding="utf-8"))
        if loc.location_id != f.parent.name:
            raise BibleError(f"{f}: location_id must match its folder name")
        locs[loc.location_id] = loc
    vpath = root / "voices.json"
    raw = json.loads(vpath.read_text(encoding="utf-8")) if vpath.exists() else {}
    voices = {cid: VoiceEntry.model_validate(v) for cid, v in raw.items() if not cid.startswith("_")}
    return Bible(root=root, characters=chars, locations=locs, voices=voices)
