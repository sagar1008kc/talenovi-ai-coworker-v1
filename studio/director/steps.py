"""Provider steps for one shot: voice (ElevenLabs) -> motion (fal reference-to-video) -> lip sync (fal)
-> speech-to-text check (ElevenLabs Scribe). Each step is cached by its output file and guarded by
EpisodeSpend before any paid call. Dry runs use local placeholders and make no network calls.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import math
import mimetypes
import os
from pathlib import Path
import subprocess
from typing import Callable

from PIL import Image, ImageDraw

from studio.director.bible import Bible
from studio.director.schema import Shot
from studio.director.spend import EpisodeSpend
from studio.director.vision import duration
from studio.media import ElevenLabsTTS, VoiceSettings
from studio.providers.base import (JobStore, ProviderError, RetryPolicy, check_response, default_http,
                                   run_job, with_retries)
from studio.providers.fal import FalQueue, image_data_uri

FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
CAMERA_TEXT = {
    "static_locked": "locked-off camera, the scene itself moves",
    "push_in": "slow dolly push-in", "pull_out": "slow pull-back", "pan_left": "smooth pan left",
    "pan_right": "smooth pan right", "tilt_up": "slow tilt up", "tilt_down": "slow tilt down",
    "dolly_left": "lateral dolly left", "dolly_right": "lateral dolly right", "orbit_left": "slow orbit left",
    "orbit_right": "slow orbit right", "handheld": "subtle handheld", "crane_up": "crane up",
    "crane_down": "crane down", "tracking": "tracking shot following the subject"}


def text_sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def ff(*args: str) -> None:
    subprocess.run([*FFMPEG, *args], check=True, capture_output=True)


def file_data_uri(path: Path, max_bytes: int = 30_000_000) -> str:
    if path.stat().st_size > max_bytes:
        raise ProviderError(f"{path.name} is too large to send inline ({path.stat().st_size} bytes)")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


@dataclass
class Steps:
    root: Path
    episode_dir: Path
    bible: Bible
    catalog: dict
    cfg: dict                                   # config/studio.json "director"
    spend: EpisodeSpend
    dry_run: bool = False
    env: dict = field(default_factory=lambda: dict(os.environ))
    log: Callable[[str], None] = print
    http: object = None

    def __post_init__(self) -> None:
        self.store = JobStore(self.episode_dir / "reports" / "jobs.json")
        if self.http is None and not self.dry_run:
            self.http = default_http()

    def shot_dir(self, shot: Shot) -> Path:
        d = self.episode_dir / "shots" / shot.shot_id / f"take{shot.attempts:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ================================================================ 1. voice
    def voice(self, shot: Shot) -> dict | None:
        """TTS with the speaker's locked voice; returns the measured duration (ffprobe)."""
        if not shot.dialogue_line.strip():
            return None
        dest = self.shot_dir(shot) / "dialogue.mp3"
        entry = self.bible.voices[shot.speaker_id]
        if self.dry_run:
            fake_voice(dest, shot.dialogue_line)
        elif not dest.is_file():
            credits = len(shot.dialogue_line)
            self.spend.guard_credits(credits, f"{shot.shot_id} voice")
            settings = VoiceSettings(env_key=entry.voice_env_key, model_id=entry.model_id,
                                     stability=entry.stability, similarity_boost=entry.similarity_boost,
                                     style=entry.style, speed=entry.speed,
                                     usd_per_1k_characters=self.spend.voice_usd_per_1k)
            tts = ElevenLabsTTS(self.http, self.env["ELEVENLABS_API_KEY"], self.env[entry.voice_env_key],
                                settings, RetryPolicy(submit_attempts=2))
            tts.synthesize(text=shot.dialogue_line, dest_audio=dest, previous_text="", next_text="",
                           ledger=self.spend.ledger)
            self.spend.note(shot.shot_id, "voice", credits / 1000 * self.spend.voice_usd_per_1k, credits=credits)
        return {"file": str(dest.relative_to(self.episode_dir)), "seconds": round(duration(dest), 3),
                "voice_env_key": entry.voice_env_key, "model_id": entry.model_id,
                "text_sha256": text_sha(shot.dialogue_line)}

    def padded_dialogue(self, shot: Shot, dialogue: Path) -> Path:
        """Dialogue placed at the lead offset inside a track exactly the shot's length (WAV)."""
        lead = self.cfg["episode_format"]["dialogue_lead_seconds"]
        out = self.shot_dir(shot) / "dialogue_shot.wav"
        ff("-i", str(dialogue), "-af",
           f"aresample=48000,aformat=channel_layouts=mono,adelay={int(lead * 1000)},"
           f"apad=whole_dur={shot.duration_sec:.3f}", "-t", f"{shot.duration_sec:.3f}", "-c:a", "pcm_s16le", str(out))
        return out

    # ================================================================ 2. motion
    def motion_prompt(self, shot: Shot) -> tuple[str, list[Path]]:
        refs: list[Path] = []
        lines = []
        for cid in shot.character_ids:
            c = self.bible.characters[cid]
            start = len(refs) + 1
            refs += self.bible.character_refs(cid)[:3]
            tags = ", ".join(f"@Image{i}" for i in range(start, len(refs) + 1))
            lines.append(f"{c.name} ({tags}): {c.age}, {c.look}; wearing {c.wardrobe}. Keep this exact face, "
                         f"hair and outfit.")
        loc = self.bible.locations[shot.location_id]
        start = len(refs) + 1
        refs += self.bible.location_plates(shot.location_id)[:2]
        tags = ", ".join(f"@Image{i}" for i in range(start, len(refs) + 1))
        style = self.bible.characters[shot.character_ids[0]].style if shot.character_ids else "cinematic"
        nobody = "" if shot.character_ids else " No people in frame."
        prompt = (f"{style} film shot, {shot.framing} framing, {CAMERA_TEXT[shot.camera_movement]}. "
                  f"Setting: {loc.name} ({tags}): {loc.description}; lighting: {loc.lighting}. "
                  + " ".join(lines) +
                  f" Action: {shot.action}. Natural acting, believable body motion and living background "
                  f"(light, air, cloth, people or objects move naturally).{nobody} Only the listed characters "
                  "appear, each exactly once. No text, captions, logos or watermarks.")
        if shot.lip_sync_required:
            speaker = self.bible.characters[shot.speaker_id]
            prompt += (f" {speaker.name} faces the camera three-quarter, mouth clearly visible, speaking "
                       "naturally.")
        return prompt, refs

    def native_lipsync(self, shot: Shot) -> bool:
        """True when the motion model itself lip-syncs to our ElevenLabs line (one call, no upload needed)."""
        key = shot.motion_model or self.cfg["motion_model"]
        spec = self.catalog["video_models"].get(key, {})
        return (shot.lip_sync_required and self.cfg.get("lipsync_mode", "post") == "native"
                and "audios" in spec.get("inputs", {}))

    def motion(self, shot: Shot, dialogue_wav: Path | None = None) -> dict:
        key = shot.motion_model or self.cfg["motion_model"]
        spec = self.catalog["video_models"].get(key)
        if not spec or spec.get("mode") != "reference_to_video":
            raise ProviderError(f"{shot.shot_id}: {key!r} is not a reference-to-video model in the catalog")
        res = self.cfg["motion_resolution"]
        allowed = spec["duration"]["values"]
        seconds = next((v for v in allowed if v >= math.ceil(shot.duration_sec)), allowed[-1])
        dest = self.shot_dir(shot) / "motion.mp4"
        prompt, refs = self.motion_prompt(shot)
        refs = refs[: spec.get("max_images", 10)]
        native = self.native_lipsync(shot) and dialogue_wav is not None
        if native:
            speaker = self.bible.characters[shot.speaker_id]
            lead = self.cfg["episode_format"]["dialogue_lead_seconds"]
            prompt += (f' @Audio1 is {speaker.name}\'s line "{shot.dialogue_line}": {speaker.name} says exactly '
                       f"this on camera, lips synced to @Audio1 (speech starts at {lead:.2f} s). Nobody else speaks.")
        info = {"file": str(dest.relative_to(self.episode_dir)), "model": spec["model_id"], "seconds": seconds,
                "resolution": res, "references": [str(r.relative_to(self.bible.root.parent)) for r in refs],
                "prompt": prompt, "native_lipsync": native}
        if self.dry_run:
            fake_motion(dest, seconds, f"{shot.shot_id} {shot.camera_movement}")
            return info
        if dest.is_file():
            return info
        usd = seconds * spec["usd_per_second"][res]
        self.spend.guard_fal(usd, f"{shot.shot_id} motion")
        fmt = spec["duration"]["format"]
        payload = dict(spec.get("fixed_args", {}))
        payload.update({"prompt": prompt, spec["inputs"]["images"]: [image_data_uri(r) for r in refs],
                        spec["duration"]["param"]: str(seconds) if fmt == "str" else seconds,
                        spec["resolution"]["param"]: res, "aspect_ratio": "9:16"})
        if native:
            audio_mp3 = self.shot_dir(shot) / "dialogue_shot.mp3"
            ff("-i", str(dialogue_wav), "-c:a", "libmp3lame", "-b:a", "192k", str(audio_mp3))
            payload[spec["inputs"]["audios"]] = [file_data_uri(audio_mp3)]
            payload["generate_audio"] = True        # audio-aware generation; our own mix replaces this track
        record = run_job(adapter=FalQueue(self.http, self.env.get("FAL_KEY", "")), http=self.http,
                         store=self.store, ledger=self.spend.ledger, key=f"motion:{shot.shot_id}:a{shot.attempts}{':lip' if native else ''}",
                         model_id=spec["model_id"], payload=payload, estimated_usd=usd,
                         item=f"{shot.shot_id} motion", dest=dest, kind="video", policy=RetryPolicy(submit_attempts=2))
        self.spend.note(shot.shot_id, "motion", usd, model=spec["model_id"], request_id=record.request_id)
        info.update(request_id=record.request_id, remote_url=record.media_url, usd=round(usd, 4))
        return info

    # ================================================================ 3. lip sync
    def lipsync(self, shot: Shot, motion_file: Path, dialogue_wav: Path, remote_video: str | None) -> dict:
        spec = self.catalog["lipsync_models"][self.cfg["lipsync_model"]]
        dest = self.shot_dir(shot) / "lipsync.mp4"
        trimmed = self.shot_dir(shot) / "motion_trim.mp4"
        ff("-i", str(motion_file), "-t", f"{shot.duration_sec:.3f}", "-an", "-c:v", "libx264", "-preset", "fast",
           "-crf", "14", str(trimmed))
        info = {"file": str(dest.relative_to(self.episode_dir)), "model": spec["model_id"]}
        if self.dry_run:
            ff("-i", str(trimmed), "-i", str(dialogue_wav), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
               "-c:a", "aac", "-shortest", str(dest))
            return info
        if dest.is_file():
            return info
        usd = shot.duration_sec * spec["usd_per_second"]
        self.spend.guard_fal(usd, f"{shot.shot_id} lipsync")
        audio_mp3 = self.shot_dir(shot) / "dialogue_shot.mp3"
        ff("-i", str(dialogue_wav), "-c:a", "libmp3lame", "-b:a", "192k", str(audio_mp3))
        payload = dict(spec.get("fixed_args", {}))
        payload.update({spec["inputs"]["video"]: self.upload(trimmed),
                        spec["inputs"]["audio"]: self.upload(audio_mp3)})
        record = run_job(adapter=FalQueue(self.http, self.env.get("FAL_KEY", "")), http=self.http,
                         store=self.store, ledger=self.spend.ledger, key=f"lipsync:{shot.shot_id}:a{shot.attempts}:cdn",
                         model_id=spec["model_id"], payload=payload, estimated_usd=usd,
                         item=f"{shot.shot_id} lipsync", dest=dest, kind="video", policy=RetryPolicy(submit_attempts=2))
        self.spend.note(shot.shot_id, "lipsync", usd, model=spec["model_id"], request_id=record.request_id)
        info.update(request_id=record.request_id, usd=round(usd, 4))
        return info

    def upload(self, path: Path) -> str:
        """Public fal CDN URL for a local file via fal's official SDK (the documented upload route;
        lip-sync endpoints reject inline data URIs)."""
        try:
            import fal_client
        except ImportError as exc:
            raise ProviderError("fal-client is not installed (pip install -r requirements.txt)") from exc
        if self.env.get("FAL_KEY") and not os.environ.get("FAL_KEY"):
            os.environ["FAL_KEY"] = self.env["FAL_KEY"]          # process-local; never printed
        url = fal_client.upload_file(str(path))
        if not str(url).startswith("https://"):
            raise ProviderError(f"fal upload of {path.name} returned no HTTPS URL")
        return url

    # ================================================================ 4. speech-to-text
    def transcribe(self, audio: Path, expected: str, item: str) -> str:
        return self.transcribe_words(audio, expected, item)[0]

    def transcribe_words(self, audio: Path, expected: str, item: str,
                         fallback_alignment: dict | None = None) -> tuple[str, list[tuple[str, float, float]]]:
        """(text, [(word, start, end)]) from ElevenLabs Scribe. Dry runs echo the expected line."""
        if self.dry_run:
            from studio.director.sync import our_words
            lead = self.cfg["episode_format"]["dialogue_lead_seconds"]
            words = [(w, s + lead, e + lead) for w, s, e in our_words(fallback_alignment)] if fallback_alignment else []
            return expected, words
        spec = self.catalog["stt_models"][self.cfg["stt_model"]]
        usd = max(10.0, duration(audio)) / 3600 * spec["usd_per_hour"]
        self.spend.guard_fal(0.0, item)          # monthly check only; STT is billed by ElevenLabs
        entry = self.spend.ledger.reserve("elevenlabs", spec["model_id"], item, usd)
        key = self.env["ELEVENLABS_API_KEY"]

        def call() -> dict:
            with audio.open("rb") as fh:
                resp = self.http.post(spec["url"], headers={"xi-api-key": key},
                                      data={"model_id": spec["model_id"],
                                            "timestamps_granularity": spec["timestamps_granularity"]},
                                      files={"file": (audio.name, fh)}, timeout=(15, 300))
            return check_response(resp, "elevenlabs speech-to-text")
        try:
            data = with_retries(call, RetryPolicy(submit_attempts=2), __import__("time").sleep, item)
            text = data["text"]
        except (ProviderError, KeyError) as exc:
            self.spend.ledger.settle(entry, "failed_unbilled", error=str(exc))
            raise
        self.spend.ledger.settle(entry, "ok")
        words = [(w["text"], float(w["start"]), float(w["end"])) for w in data.get("words", [])
                 if w.get("type") == "word"]
        return text, words


# ==================================================================== dry-run placeholders
def fake_voice(dest: Path, text: str) -> None:
    seconds = max(1.0, len(text.split()) / 2.8)
    ff("-f", "lavfi", "-i", f"sine=frequency=210:duration={seconds:.3f}", "-af", "volume=0.25",
       "-c:a", "libmp3lame", str(dest))
    step = seconds / max(1, len(text))
    dest.with_suffix(".alignment.json").write_text(json.dumps({
        "characters": list(text), "character_start_times_seconds": [i * step for i in range(len(text))],
        "character_end_times_seconds": [(i + 1) * step for i in range(len(text))]}), encoding="utf-8")


def fake_motion(dest: Path, seconds: int, label: str, w: int = 720, h: int = 1280) -> None:
    """Moving test pattern with a label: real frame-to-frame motion, clearly not paid media."""
    card = dest.with_name("label.png")
    img = Image.new("RGBA", (w, 160), (0, 0, 0, 160))
    ImageDraw.Draw(img).text((24, 50), f"DRY RUN  {label}", fill=(255, 255, 255, 255))
    img.save(card)
    ff("-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={seconds}", "-i", str(card),
       "-filter_complex", "[0:v][1:v]overlay=0:H*0.45,format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
       str(dest))
    card.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
