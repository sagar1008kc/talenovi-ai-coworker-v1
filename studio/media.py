"""Narration (ElevenLabs with timestamps) and licensed background-music selection."""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
import time
from typing import Callable

from pydantic import BaseModel

from studio.budget import CostLedger
from studio.providers.base import HttpClient, ProviderError, RetryPolicy, check_response, with_retries

log = logging.getLogger("studio.media")

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"


class VoiceSettings(BaseModel):
    env_key: str = "ELEVENLABS_VOICE_ID"
    model_id: str = "eleven_multilingual_v2"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    speed: float = 1.0
    usd_per_1k_characters: float | None = None
    commercial_license_confirmed: bool = False


class ElevenLabsTTS:
    """POST /v1/text-to-speech/{voice_id}/with-timestamps -> audio_base64 + character alignment."""

    def __init__(self, http: HttpClient, api_key: str, voice_id: str, settings: VoiceSettings,
                 policy: RetryPolicy, sleep: Callable[[float], None] = time.sleep):
        if not api_key or not voice_id:
            raise ProviderError("ELEVENLABS_API_KEY and the configured voice ID env var are required")
        if settings.usd_per_1k_characters is None:
            raise ProviderError("Set voice.usd_per_1k_characters from your ElevenLabs plan so the "
                                "budget can be enforced")
        self.http, self.api_key, self.voice_id = http, api_key, voice_id
        self.settings, self.policy, self.sleep = settings, policy, sleep

    provider = "elevenlabs"

    def estimate_usd(self, text: str) -> float:
        return len(text) / 1000 * float(self.settings.usd_per_1k_characters or 0)

    def synthesize(self, *, text: str, dest_audio: Path, previous_text: str, next_text: str,
                   ledger: CostLedger) -> tuple[Path, dict]:
        """Cache-aware: an existing audio+alignment pair is never regenerated."""
        align_path = dest_audio.with_suffix(".alignment.json")
        if dest_audio.is_file() and align_path.is_file():
            return dest_audio, json.loads(align_path.read_text(encoding="utf-8"))
        entry = ledger.reserve(self.provider, self.settings.model_id, f"tts {dest_audio.stem}",
                               self.estimate_usd(text), characters=len(text))
        body = {
            "text": text,
            "model_id": self.settings.model_id,
            "previous_text": previous_text[-1000:],
            "next_text": next_text[:1000],
            "voice_settings": {"stability": self.settings.stability,
                               "similarity_boost": self.settings.similarity_boost,
                               "style": self.settings.style, "speed": self.settings.speed},
        }

        def call() -> dict:
            resp = self.http.post(TTS_URL.format(voice_id=self.voice_id),
                                  params={"output_format": "mp3_44100_128"}, json=body,
                                  headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
                                  timeout=(15, 300))
            return check_response(resp, "elevenlabs tts")

        try:
            data = with_retries(call, self.policy, self.sleep, f"tts {dest_audio.stem}")
            audio = base64.b64decode(data["audio_base64"])
            alignment = data.get("alignment") or data.get("normalized_alignment")
            if not audio or not alignment:
                raise ProviderError("TTS response missing audio or alignment")
        except (ProviderError, KeyError, ValueError) as exc:
            ledger.settle(entry, "failed_unbilled", error=str(exc))
            raise
        dest_audio.parent.mkdir(parents=True, exist_ok=True)
        dest_audio.write_bytes(audio)
        align_path.write_text(json.dumps(alignment), encoding="utf-8")
        ledger.settle(entry, "ok")
        return dest_audio, alignment


class MusicTrack(BaseModel):
    file: str
    title: str
    license: str
    commercial_use_confirmed: bool
    source_url: str = ""
    mood: str = ""


class MusicLibrary:
    """User-supplied licensed tracks listed in <library>/provenance.json; never fetched online.

    provenance.json: {"tracks": [{"file": "calm.mp3", "title": "...", "license": "...",
                                  "commercial_use_confirmed": true, "source_url": "..."}]}
    """

    def __init__(self, library_dir: Path):
        self.dir = library_dir
        self.tracks: list[MusicTrack] = []
        manifest = library_dir / "provenance.json"
        if manifest.is_file():
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            self.tracks = [MusicTrack.model_validate(t) for t in raw.get("tracks", [])]

    def usable(self) -> list[MusicTrack]:
        return [t for t in self.tracks if t.commercial_use_confirmed and (self.dir / t.file).is_file()]

    def pick(self, seed: str, recently_used: list[str]) -> MusicTrack | None:
        """Deterministic choice that avoids recently used tracks where possible."""
        options = self.usable()
        if not options:
            return None
        fresh = [t for t in options if t.file not in recently_used] or options
        return fresh[sum(map(ord, seed)) % len(fresh)]
