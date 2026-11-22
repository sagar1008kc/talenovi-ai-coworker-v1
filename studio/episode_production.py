"""Paid production of ONE series episode into a single 9:16 master (autonomous-mode capable).

- hard per-episode caps (fal USD, ElevenLabs credits) and a monthly cap, reserved before every paid call;
- per-scene ElevenLabs narration with character alignment -> word-timed burned-in captions;
- several FLUX 2 Flash shots per scene, Wan image-to-video for storyboard motion shots;
- camera moves on stills, story-aware transitions, a unifying film look, loudness-normalized AAC;
- technical QC, cost/generation/quality reports and a resumable job store (never pays twice).

Captions are drawn with Pillow and overlaid, so FFmpeg builds without libass/freetype work.
Nothing is uploaded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps

from studio.budget import BudgetCaps, CostLedger, MonthlySpend, RunSpend
from studio.captions import Cue, build_cues, words_from_alignment, write_srt
from studio.media import ElevenLabsTTS, VoiceSettings
from studio.models import SHORT_FORM, RenderProfile
from studio.providers import (JobStore, ProviderError, RetryPolicy, VideoRequest, build_image_generator,
                              build_video_providers, generate_clip, load_catalog)
from studio.providers.base import default_http
from studio.qc import Check, caption_checks, repeated_visual_checks, technical_checks
from studio.series import SeriesBible, get_series, write_json
from studio.story_checks import preflight
from studio.music import build_prompt, compose_ai_score, library_track, music_credits
from studio.voices import missing_speaker_voices
from studio.stories import (CAPTION_ZONES, ScratchManifest, duration_limits, duration_verdict, dry_run_target,
                            is_story_episode)

FPS = 30
GAP_S = 0.45          # silence before each scene's line; the scene transition happens inside it
TAIL_S = 1.4          # held final image after the last line
STILL_SIZE = {"width": 1088, "height": 1920}   # FLUX multiple of 16; centre-cropped to 1080x1920
MOTION_MODEL_ORDER = ["wan3_i2v"]               # only the authorized provider; no paid fallbacks
MOTION_RESOLUTION = "720p"
MOVES = ("push_in", "pull_out", "tilt_up", "tilt_down", "drift_left", "drift_right")
MOVE_TO_CAMERA = {"push_in": "zoom_in", "pull_out": "zoom_out", "tilt_up": "push_slow",
                  "tilt_down": "push_slow", "drift_left": "pan_left", "drift_right": "pan_right"}
# storyboard name -> (ffmpeg xfade transition, seconds). Shots inside a scene default to "dissolve";
# a new scene defaults to "fade"; act breaks / time jumps should use "fadeblack".
TRANSITIONS = {"cut": ("fade", 0.12), "dissolve": ("dissolve", 0.35), "fade": ("fade", 0.6),
               "fadeblack": ("fadeblack", 0.9), "smoothleft": ("smoothleft", 0.5),
               "smoothright": ("smoothright", 0.5), "smoothup": ("smoothup", 0.5),
               "circleopen": ("circleopen", 0.6), "fade_from_black": ("fade", 0.6)}
# Procedural ambience: (noise colour, default level dB, filter chain). Quiet beds that carry place across cuts.
ROOM_TONES = {
    "shop": ("brown", -36.0, "highpass=f=60,lowpass=f=900,tremolo=f=0.15:d=0.25"),
    "rain": ("pink", -31.0, "highpass=f=400,lowpass=f=7000,tremolo=f=0.6:d=0.15"),
    "night": ("brown", -38.0, "highpass=f=40,lowpass=f=500"),
}
FILM_LOOK = "noise=alls=4:allf=t,vignette=angle=PI/7"
FONT_CANDIDATES = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                   "/Library/Fonts/Arial Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "C:/Windows/Fonts/arialbd.ttf"]


class SpendCapExceeded(RuntimeError):
    """Raised BEFORE a paid call that would exceed a cap."""


class DurationOutOfRange(ValueError):
    """The measured voice track puts the episode outside the configured runtime range (before image spend)."""


REQUIRED_MOTION_RETRIES = 2   # extra attempts per motion_required shot, across runs, each guarded by the fal cap


# ============================================================================ config
def load_studio_config(root: Path) -> dict:
    path = root / "config" / "studio.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


# ============================================================================ pure helpers
def episode_profile(width: int = 1080, height: int = 1920, limits: dict | None = None) -> RenderProfile:
    """The fiction master: native 9:16; runtime range from config/studio.json "format" (default 60-90 s)."""
    limits = limits or {"min_seconds": 60.0, "max_seconds": 90.0}
    return SHORT_FORM.model_copy(update={"width": width, "height": height, "min_seconds": limits["min_seconds"],
                                         "max_seconds": limits["max_seconds"], "filename_suffix": "master_9x16"})


def story_sha256(folder: Path) -> str:
    return hashlib.sha256((folder / "story.json").read_bytes()).hexdigest()


@dataclass
class Timeline:
    """Scene windows on the final timeline. Window i = GAP + spoken line i (+ TAIL on the last)."""
    audio_seconds: list[float]
    gap: float = GAP_S
    tail: float = TAIL_S
    holds: list[float] | None = None  # optional silence after each scene's line (reaction beats)

    def hold(self, i: int) -> float:
        return float(self.holds[i]) if self.holds and i < len(self.holds) else 0.0

    @property
    def windows(self) -> list[float]:
        w = [self.gap + d + self.hold(i) for i, d in enumerate(self.audio_seconds)]
        if w:
            w[-1] += self.tail
        return w

    @property
    def starts(self) -> list[float]:
        return cumulative(self.windows)

    @property
    def speech_starts(self) -> list[float]:
        return [s + self.gap for s in self.starts]

    @property
    def total(self) -> float:
        return sum(self.windows)


MAX_HOLD_S = 2.5     # longest reaction silence a storyboard may hold after a line


def scene_holds(board: dict, n_scenes: int) -> list[float]:
    """storyboard `scene_holds` {"S04": 1.2} -> per-scene silence after the line, clamped to 0..MAX_HOLD_S."""
    raw = board.get("scene_holds") or {}
    out = [0.0] * n_scenes
    for key, value in raw.items():
        i = int(str(key).lstrip("Ss")) - 1
        if not 0 <= i < n_scenes:
            raise ValueError(f"scene_holds: unknown scene {key!r}")
        out[i] = max(0.0, min(MAX_HOLD_S, float(value)))
    return out


def cumulative(windows: list[float]) -> list[float]:
    out, t = [], 0.0
    for w in windows:
        out.append(t)
        t += w
    return out


def shot_windows(scene_windows: list[float], shot_scenes: list[int], weights: list[float]) -> list[float]:
    """Split each scene window among its shots by weight. Shots must be in scene order."""
    if sorted(shot_scenes) != shot_scenes or set(shot_scenes) != set(range(len(scene_windows))):
        raise ValueError("Every scene needs at least one shot, and shots must be in scene order")
    out = []
    for i, scene in enumerate(shot_scenes):
        total_w = sum(w for s, w in zip(shot_scenes, weights) if s == scene)
        out.append(scene_windows[scene] * weights[i] / total_w)
    return out


def xfade_plan(windows: list[float], durations: list[float]) -> tuple[list[float], list[float]]:
    """Segment lengths and xfade offsets that keep total length == sum(windows).

    durations[k] is the transition INTO shot k (durations[0] is ignored). Segment i is extended by the
    duration of the transition that follows it; the k-th xfade starts exactly at shot k's start.
    """
    n = len(windows)
    lengths = [windows[i] + (durations[i + 1] if i + 1 < n else 0.0) for i in range(n)]
    for i in range(1, n):
        if durations[i] >= min(windows[i - 1], windows[i]):
            raise ValueError(f"Transition into shot {i} is longer than a neighbouring shot")
    return lengths, cumulative(windows)[1:]


def transition_for(shot: dict, index: int, new_scene: bool) -> tuple[str, float]:
    name = shot.get("transition_in") or ("fade" if new_scene else "dissolve")
    if index > 0 and name == "fade_from_black":
        name = "fade"
    return TRANSITIONS.get(name, TRANSITIONS["fade" if new_scene else "dissolve"])


def caption_timeline(cues: list[Cue], total: float) -> list[tuple[int | None, float]]:
    """(cue index or None for blank, duration) items covering [0, total] exactly."""
    items: list[tuple[int | None, float]] = []
    t = 0.0
    for i, cue in enumerate(cues):
        start, end = max(cue.start, t), min(cue.end, total)
        if end <= start:
            continue
        if start - t > 1e-3:
            items.append((None, start - t))
        items.append((i, end - start))
        t = end
    if total - t > 1e-3:
        items.append((None, total - t))
    return items


def normalize_move(text: str, index: int) -> str:
    t = (text or "").lower()
    if t in MOVES:
        return t
    for key, words in (("pull_out", ("pull back", "pull out", "reveal")), ("tilt_up", ("tilt up", "upward")),
                       ("tilt_down", ("downward", "tilt down", "drawing down")),
                       ("drift_left", ("orbit", "pan left", "drift left")),
                       ("drift_right", ("pan right", "drift right"))):
        if any(w in t for w in words):
            return key
    return "push_in" if index % 2 == 0 else "pull_out"


def zoompan_expr(move: str, frames: int, w: int, h: int) -> str:
    n = max(1, frames - 1)
    p = f"(on/{n})"
    z, x, y = "1.0", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if move == "push_in":
        z = f"1.0+0.08*{p}"
    elif move == "pull_out":
        z = f"1.08-0.08*{p}"
    elif move in ("tilt_up", "tilt_down"):
        z = "1.10"
        y = f"(ih-ih/zoom)*(1-{p})" if move == "tilt_up" else f"(ih-ih/zoom)*{p}"
    elif move in ("drift_left", "drift_right"):
        z = "1.10"
        x = f"(iw-iw/zoom)*(1-{p})" if move == "drift_left" else f"(iw-iw/zoom)*{p}"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={FPS}"


# Framing leads the prompt: FLUX otherwise drifts to wide ensemble shots of the whole set.
FRAMING = {
    "insert": "Extreme close-up insert shot of hands and objects only; no faces, no full figures.",
    "extreme_close": "Extreme close-up insert shot of hands and objects only; no faces, no full figures.",
    "close": "Tight close-up framing on one face, head and shoulders, background softly out of focus.",
    "close-up": "Tight close-up framing on one face, head and shoulders, background softly out of focus.",
    "medium": "Medium shot, waist up.",
    "over_shoulder": "Over-the-shoulder shot.",
    "two_shot": "Two-shot of exactly two people.",
    "wide": "Wide shot.",
}


def image_prompt(bible: SeriesBible, scene: Any, shot: dict) -> str:
    cast = {c.id: c for c in bible.characters}
    ids = [c for c in shot.get("character_ids", scene.character_ids) if c in cast]
    visual = shot.get("image_prompt") or shot.get("visual_description") or scene.visual_prompt
    size = shot.get("shot_size", "")
    hands_only = size in ("insert", "extreme_close")
    anchors = []
    for cid in ([] if hands_only else ids):  # an insert shows hands; a full-body anchor would add a person
        ch = cast[cid]
        # If the shot already names the character, it already states their outfit for this moment. Adding the
        # bible's multi-outfit wardrobe text makes the model draw the same person twice (seen in review).
        named = ch.name.split()[0].lower() in visual.lower()
        anchors.append(f"{ch.name} ({ch.age_range}): {ch.appearance}." if named else
                       f"{ch.name} ({ch.age_range}): {ch.appearance}; wearing {ch.signature_wardrobe}.")
    if hands_only:
        people = "No faces and no full figures; only the hands and objects described."
    elif len(ids) == 1:
        people = "Exactly one person in the image; nobody else in the room or background."
    elif ids:
        people = (f"Exactly {len(ids)} people in the image, each appearing once; no duplicated people, "
                  "no extra people in the background.")
    else:
        people = "No people in the image."
    framing = FRAMING.get(size, "")
    return (f"{framing} {visual}. {' '.join(anchors)} {people} Style: {bible.visual_style}. Continuity: "
            f"{scene.visual_continuity_notes} Vertical 9:16 cinematic film still, key subject inside the central "
            "safe area, wholly original fictional people, no text, no letters, no numbers, no logos, no watermark."
            ).strip()


def scene_index(shot: dict) -> int:
    return int(str(shot["scene_id"]).lstrip("Ss")) - 1


# ============================================================================ production
@dataclass
class Production:
    root: Path
    folder: Path
    max_fal_usd: float
    max_voice_credits: int
    motion_seconds: int = 30
    monthly_cap_usd: float | None = None
    width: int = 1080
    height: int = 1920
    dry_run: bool = False
    voice_usd_per_1k: float = 0.22
    music_default: str = "ai"          # ai | library | none (storyboard "music.source" overrides)
    env: dict = field(default_factory=lambda: dict(os.environ))
    log: Callable[[str], None] = print

    def p(self, *parts: str) -> Path:
        path = self.folder.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def story_layout(self) -> bool:
        return is_story_episode(self.folder)

    def limits(self) -> dict:
        return duration_limits(self.root)

    def scratch(self, paths: list[Path] | Path, reason: str, kind: str = "file") -> None:
        """List workflow-created disposable files in this run's manifest (cleaned only after release)."""
        manifest = ScratchManifest(self.folder)
        manifest.add(paths if isinstance(paths, list) else [paths], reason, kind)
        manifest.save()

    # ---------------------------------------------------------------- gates
    def check_gates(self, confirm_spend: bool) -> tuple[Any, SeriesBible]:
        from main import load_story  # local import: main imports this module lazily
        if not confirm_spend and not self.dry_run:
            raise ValueError("Paid generation requires --confirm-spend (consent flag, not a cap)")
        for binary in ("ffmpeg", "ffprobe"):
            if shutil.which(binary) is None:
                raise RuntimeError(f"Install {binary} and add it to PATH")
        status = json.loads((self.folder / "status.json").read_text(encoding="utf-8"))
        if not status.get("script_approved") or status.get("approved_story_sha256") != story_sha256(self.folder):
            raise ValueError("Script not approved, or changed after approval. Run approve-script --confirm-reviewed")
        story = load_story(self.folder)
        if not story.series_slug:
            raise ValueError("produce-episode is for series episodes; use produce for one-offs")
        bible, ledger = get_series(self.root, story.series_slug)
        check = preflight(story, bible, ledger, root=self.root)
        write_json(self.folder / "story_preflight.json", check)
        if not check["passed"]:
            raise ValueError(f"Series preflight failed: {check['errors']}")
        if not self.dry_run:
            for name in ("FAL_KEY", "ELEVENLABS_API_KEY"):
                if not self.env.get(name):
                    raise RuntimeError(f"Set {name} in .env")
            missing = missing_speaker_voices(bible, self.env, {s.speaker_id for s in story.scenes})
            if missing:
                raise RuntimeError(f"Voice roster incomplete; set in .env: {missing}")
        return story, bible

    def load_shots(self, story: Any) -> tuple[dict, list[dict]]:
        board = json.loads((self.folder / "storyboard.json").read_text(encoding="utf-8"))
        shots = board["shots"]
        groups = [scene_index(s) for s in shots]
        if sorted(groups) != groups or set(groups) != set(range(len(story.scenes))):
            raise ValueError("storyboard.json: every scene needs >= 1 shot and shots must be in scene order")
        return board, shots

    # ---------------------------------------------------------------- budget
    def ledger(self) -> CostLedger:
        voice_usd_cap = self.max_voice_credits / 1000 * self.voice_usd_per_1k
        cap = self.max_fal_usd + voice_usd_cap
        month = MonthlySpend(self.root / "state")
        month_cap = self.monthly_cap_usd if self.monthly_cap_usd is not None else month.total() + cap
        caps = BudgetCaps(max_usd_per_video=cap, max_usd_per_run=cap, max_usd_per_month=month_cap)
        return CostLedger(self.p("reports", "cost_report.json"), caps, RunSpend(), month)

    @staticmethod
    def fal_spent(ledger: CostLedger) -> float:
        return ledger.by_provider().get("fal", 0.0)

    @staticmethod
    def credits_spent(ledger: CostLedger) -> int:
        return sum(int(e.detail.get("credits", e.detail.get("characters", 0))) for e in ledger.entries
                   if e.provider == "elevenlabs" and e.status != "failed_unbilled")

    def guard_fal(self, ledger: CostLedger, estimate: float, item: str) -> None:
        spent = self.fal_spent(ledger)
        if spent + estimate > self.max_fal_usd + 1e-9:
            raise SpendCapExceeded(f"{item}: ${estimate:.3f} would exceed fal cap "
                                   f"(${spent:.3f} of ${self.max_fal_usd:.2f} used)")

    def guard_voice(self, ledger: CostLedger, characters: int, item: str) -> None:
        spent = self.credits_spent(ledger)
        if spent + characters > self.max_voice_credits:
            raise SpendCapExceeded(f"{item}: {characters} credits would exceed ElevenLabs cap "
                                   f"({spent} of {self.max_voice_credits} used)")

    def clip_seconds(self, n: int) -> int:
        return max(2, min(10, self.motion_seconds // n)) if n else 0

    def estimate(self, story: Any, shots: list[dict], motion: list[int]) -> dict:
        catalog = load_catalog(self.root)
        mp = math.ceil(STILL_SIZE["width"] * STILL_SIZE["height"] / 1_000_000 * 100) / 100
        image_usd = len(shots) * mp * catalog["image_models"]["flux2_flash"]["usd_per_megapixel"]
        per_clip = self.clip_seconds(len(motion))
        rate = catalog["video_models"][MOTION_MODEL_ORDER[0]]["usd_per_second"][MOTION_RESOLUTION]
        motion_usd = len(motion) * per_clip * rate
        chars = sum(len(s.narration) for s in story.scenes)
        # Speech runs about 13 characters/s; add the scene gaps and tail. Capped at the configured maximum.
        est_seconds = min(self.limits()["max_seconds"], chars / 13.0 + GAP_S * len(story.scenes) + TAIL_S)
        music_est = music_credits(est_seconds) if self.music_source() == "ai" else 0
        return {"music_credits_estimate": music_est,"scenes": len(story.scenes), "images": len(shots), "image_usd": round(image_usd, 4),
                "motion_clips": len(motion), "seconds_per_clip": per_clip,
                "motion_usd": round(motion_usd, 4), "fal_total_usd": round(image_usd + motion_usd, 4),
                "voice_characters": chars, "voice_credits_estimate": chars + music_est,
                "fal_cap_usd": self.max_fal_usd, "voice_credit_cap": self.max_voice_credits,
                "monthly_cap_usd": self.monthly_cap_usd}

    # ---------------------------------------------------------------- run
    def run(self, confirm_spend: bool) -> Path:
        story, bible = self.check_gates(confirm_spend)
        board, shots = self.load_shots(story)
        self.music_spec = board.get("music", {})
        self.board = board
        motion = ([i for i, s in enumerate(shots) if s.get("motion_candidate") or s.get("motion_required")]
                  if self.motion_seconds else [])
        est = self.estimate(story, shots, motion)
        write_json(self.p("reports", "estimate.json"), est)
        self.log(f"ESTIMATE: {json.dumps(est)}")
        if est["fal_total_usd"] > self.max_fal_usd or est["voice_credits_estimate"] > self.max_voice_credits:
            raise SpendCapExceeded(f"Estimate exceeds the caps: {est}")

        ledger = self.ledger()
        store = JobStore(self.p("reports", "jobs.json"))
        self.update_status("GENERATING_MEDIA")
        balance_before = self.voice_balance()
        if balance_before and balance_before["remaining"] < est["voice_credits_estimate"]:
            raise SpendCapExceeded(f"ElevenLabs balance too low: {balance_before}")

        audio = self.make_voice(story, bible, ledger)
        timeline = Timeline([a["duration_seconds"] for a in audio], holds=scene_holds(board, len(story.scenes)))
        self.check_runtime(timeline)
        windows = shot_windows(timeline.windows, [scene_index(s) for s in shots],
                               [float(s.get("weight", 1.0)) for s in shots])
        images = self.make_images(story, bible, shots, ledger, store)
        clips, motion_notes = self.make_clips(story, shots, motion, images, ledger, store, windows)
        balance_after = self.voice_balance()
        narration = self.mix_narration(audio, timeline)
        cues = self.captions(audio, timeline)
        music, music_meta = self.make_music(timeline, ledger)
        room = self.make_room_tone(timeline)
        master = self.render(shots, images, clips, windows, timeline, narration, cues, music, room)
        self.music_meta = music_meta
        return self.finish(story, shots, images, clips, motion_notes, audio, timeline, windows, cues, master,
                           ledger, est, balance_before, balance_after)

    def check_runtime(self, timeline: Timeline) -> None:
        """Gate on measured speech BEFORE images/clips are paid for; voice tracks stay cached for the revision."""
        verdict, detail = duration_verdict(timeline.total, self.limits())
        write_json(self.p("reports", "runtime_check.json"), {"measured_seconds": round(timeline.total, 2),
                                                             "verdict": verdict, "detail": detail})
        if verdict == "fail":
            self.update_status("DURATION_OUT_OF_RANGE", measured_seconds=round(timeline.total, 2))
            raise DurationOutOfRange(f"Voice track runtime {detail}. Revise the narration (cut or add lines), "
                                     "re-run approve-script and produce again; no images were generated.")
        self.log(f"runtime {detail}" + ("" if verdict == "pass" else " (outside target window)"))

    # ---------------------------------------------------------------- voice
    def voice_balance(self) -> dict | None:
        if self.dry_run:
            return None
        import requests
        r = requests.get("https://api.elevenlabs.io/v1/user/subscription",
                         headers={"xi-api-key": self.env["ELEVENLABS_API_KEY"]}, timeout=30)
        if not r.ok:
            return None
        d = r.json()
        return {"used": d.get("character_count"), "limit": d.get("character_limit"),
                "remaining": (d.get("character_limit") or 0) - (d.get("character_count") or 0)}

    def make_voice(self, story: Any, bible: SeriesBible, ledger: CostLedger) -> list[dict]:
        out: list[dict] = []
        http = default_http()
        texts = [s.narration for s in story.scenes]
        for i, scene in enumerate(story.scenes):
            dest = self.p("audio", "audio_segments", f"scene_{i + 1:02}.mp3")
            align_path = dest.with_suffix(".alignment.json")
            profile = bible.voices[scene.speaker_id]
            source = {"text_sha256": hashlib.sha256(scene.narration.encode("utf-8")).hexdigest(),
                      "speaker_id": scene.speaker_id, "env_key": profile.env_key}
            source_path = dest.with_suffix(".source.json")
            if dest.is_file() and source_path.is_file() and \
                    json.loads(source_path.read_text(encoding="utf-8")) != source:
                # The script line changed after this take was paid for: keep the take as rejected scratch.
                rejected = self.p("audio", "rejected", f"{dest.stem}_{int(dest.stat().st_mtime)}.mp3")
                dest.replace(rejected)
                align_path.unlink(missing_ok=True)
                source_path.unlink()
                self.scratch(rejected, "voice take for a revised line (rejected take)")
            if self.dry_run:
                alignment = fake_voice(dest, scene.narration)
            elif dest.is_file() and align_path.is_file():
                alignment = json.loads(align_path.read_text(encoding="utf-8"))
            else:
                self.guard_voice(ledger, len(scene.narration), f"tts scene {i + 1}")
                settings = VoiceSettings(env_key=profile.env_key, model_id=profile.model_id,
                                         stability=profile.stability, similarity_boost=profile.similarity_boost,
                                         style=profile.style, speed=profile.speed,
                                         usd_per_1k_characters=self.voice_usd_per_1k)
                tts = ElevenLabsTTS(http, self.env["ELEVENLABS_API_KEY"], self.env[profile.env_key], settings,
                                    RetryPolicy(submit_attempts=2))
                # Only same-speaker neighbours are passed as context, so prosody never bleeds across voices.
                prev = texts[i - 1] if i and story.scenes[i - 1].speaker_id == scene.speaker_id else ""
                nxt = (texts[i + 1] if i + 1 < len(texts) and story.scenes[i + 1].speaker_id == scene.speaker_id
                       else "")
                _, alignment = tts.synthesize(text=scene.narration, dest_audio=dest, previous_text=prev,
                                              next_text=nxt, ledger=ledger)
                self.log(f"voice {i + 1}/{len(texts)} ({scene.speaker_id})")
            write_json(source_path, source)
            out.append({"scene": i + 1, "speaker_id": scene.speaker_id, "voice_env_key": profile.env_key,
                        "model_id": profile.model_id, "file": str(dest.relative_to(self.folder)),
                        "characters": len(scene.narration), "duration_seconds": round(probe_seconds(dest), 3),
                        "alignment": alignment})
        return out

    def mix_narration(self, audio: list[dict], tl: Timeline) -> Path:
        """One narration track: GAP before every line, TAIL at the end, 48 kHz stereo WAV + MP3."""
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        parts = []
        for i, a in enumerate(audio):
            args += ["-i", str(self.folder / a["file"])]
            pad = tl.hold(i) + (tl.tail if i == len(audio) - 1 else 0.0)
            ms = round(tl.gap * 1000)
            parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},"
                         f"apad=pad_dur={pad:.3f},atrim=0:{tl.windows[i]:.4f}[a{i}]")
        graph = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(len(audio))) + \
            f"concat=n={len(audio)}:v=0:a=1[out]"
        wav = self.p("audio", "narration.wav")
        run(*args, "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", str(wav))
        self.scratch(wav, "temporary narration mix (rebuilt free from cached voice tracks)")
        if not self.story_layout:  # legacy folders keep their MP3 preview; story folders keep only the master
            mp3 = self.p("audio", "narration.mp3")
            run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "libmp3lame",
                "-b:a", "192k", str(mp3))
            self.scratch(mp3, "narration preview transcode")
        return wav

    def captions(self, audio: list[dict], tl: Timeline) -> list[Cue]:
        words = []
        for a, start in zip(audio, tl.speech_starts):
            words += words_from_alignment(a["alignment"], offset=start)
        cues = build_cues(words, max_words=3, max_chars=22)
        write_srt(cues, self.p("captions.srt"))
        return cues

    # ---------------------------------------------------------------- music
    def music_source(self) -> str:
        return (getattr(self, "music_spec", {}) or {}).get("source", self.music_default)

    def make_music(self, tl: Timeline, ledger: CostLedger) -> tuple[Path | None, dict]:
        spec, source = getattr(self, "music_spec", {}) or {}, self.music_source()
        if source == "none":
            return None, {"source": "none"}
        if source == "library":
            path, meta = library_track(self.root, spec["file"])
            return path, meta
        dest = self.p("audio", "music", "score.mp3")
        prompt = build_prompt(spec, tl.starts, tl.total)
        if self.dry_run:
            if not dest.is_file():
                run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=330:duration={tl.total:.2f}", "-af", "volume=0.2", str(dest))
            return dest, {"source": "dry_run", "prompt": prompt}
        try:
            meta = compose_ai_score(api_key=self.env["ELEVENLABS_API_KEY"], prompt=prompt, seconds=tl.total,
                                    dest=dest, ledger=ledger, guard=lambda c, item: self.guard_voice(ledger, c, item))
            self.log("music score done")
            return dest, meta
        except SpendCapExceeded:
            raise
        except Exception as exc:  # reported in the generation report, never silent
            self.log(f"music FAILED, rendering without score: {exc}")
            return None, {"source": "failed", "error": str(exc)}

    # ---------------------------------------------------------------- room tone
    def make_room_tone(self, tl: Timeline) -> Path | None:
        """Procedural ambience bed (FFmpeg noise, generated locally, no provider, no licence needed).

        storyboard `room_tone`: {"preset": "shop"|"rain"|"night", "level_db": -34}. Absent = no bed.
        """
        spec = (getattr(self, "board", {}) or {}).get("room_tone")
        if not spec:
            return None
        preset = spec.get("preset", "shop")
        if preset not in ROOM_TONES:
            raise ValueError(f"room_tone preset must be one of {sorted(ROOM_TONES)}")
        level = max(-48.0, min(-24.0, float(spec.get("level_db", ROOM_TONES[preset][1]))))
        dest = self.p("audio", "room_tone.wav")
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            f"anoisesrc=color={ROOM_TONES[preset][0]}:sample_rate=48000:amplitude=0.5:duration={tl.total + 0.5:.2f}"
            f":seed=7", "-af", f"{ROOM_TONES[preset][2]},aformat=channel_layouts=stereo,volume={level}dB",
            "-ar", "48000", str(dest))
        self.scratch([dest], "procedural room tone (regenerated on render)")
        self.room_tone_meta = {"source": "procedural_ffmpeg", "preset": preset, "level_db": level,
                               "license": "generated locally; no third-party audio"}
        return dest

    # ---------------------------------------------------------------- visuals
    def make_images(self, story: Any, bible: SeriesBible, shots: list[dict], ledger: CostLedger,
                    store: JobStore) -> list[Path]:
        paths = []
        gen = None if self.dry_run else build_image_generator(load_catalog(self.root), "flux2_flash",
                                                              RetryPolicy(), env=self.env)
        for i, shot in enumerate(shots):
            scene = story.scenes[scene_index(shot)]
            dest = self.p("visuals", "images", f"{shot['shot_id']}.png")
            prompt = image_prompt(bible, scene, shot)
            if self.dry_run:
                fake_image(dest, i)
            elif not dest.is_file():
                self.guard_fal(ledger, gen.estimate_usd(STILL_SIZE), f"image {shot['shot_id']}")
                gen.generate(key=f"image:{shot['shot_id']}", prompt=prompt, size=STILL_SIZE, dest=dest,
                             store=store, ledger=ledger)
                self.log(f"image {i + 1}/{len(shots)}")
            shot["image_prompt_sent"] = prompt
            paths.append(dest)
        return paths

    def make_clips(self, story: Any, shots: list[dict], motion: list[int], images: list[Path],
                   ledger: CostLedger, store: JobStore,
                   windows: list[float] | None = None) -> tuple[dict[int, Path], list[dict]]:
        clips: dict[int, Path] = {}
        notes: list[dict] = []
        if not motion:
            return clips, notes
        budget_seconds = self.clip_seconds(len(motion))
        providers = [] if self.dry_run else build_video_providers(load_catalog(self.root), MOTION_MODEL_ORDER,
                                                                  env=self.env)
        if not providers and not self.dry_run:
            raise RuntimeError("Wan image-to-video is not available (FAL_KEY missing?)")
        for i in motion:
            shot = shots[i]
            scene = story.scenes[scene_index(shot)]
            # Pay only for what the shot shows: ceil(shot length), at least 3 s, at most the per-clip budget.
            seconds = budget_seconds if windows is None else max(3, min(budget_seconds, math.ceil(windows[i])))
            dest = self.p("visuals", "video_clips", f"{shot['shot_id']}.mp4")
            move = normalize_move(shot.get("camera_movement", ""), i)
            req = VideoRequest(shot_id=shot["shot_id"], mode="image_to_video",
                               prompt=(f"{shot.get('video_prompt') or ''}. "
                                       f"{shot.get('visual_description') or scene.visual_prompt}. "
                                       "Cinematic, realistic, gentle natural motion, faces and clothing unchanged."),
                               image_path=images[i], seconds_needed=seconds, aspect_ratio="9:16",
                               resolution=MOTION_RESOLUTION, camera=MOVE_TO_CAMERA[move])
            if self.dry_run:
                fake_clip(images[i], dest, seconds, self.width, self.height)
                clips[i] = dest
                notes.append({"shot_id": shot["shot_id"], "status": "dry_run"})
                continue
            if dest.is_file():
                clips[i] = dest
                notes.append({"shot_id": shot["shot_id"], "status": "cached"})
                continue
            required = bool(shot.get("motion_required"))
            while True:
                self.guard_fal(ledger, providers[0].estimate_usd(req), f"clip {shot['shot_id']}")
                try:
                    provider, record = generate_clip(providers, req, http=default_http(), store=store,
                                                     ledger=ledger, dest=dest,
                                                     policy=RetryPolicy(submit_attempts=2))
                    clips[i] = dest
                    notes.append({"shot_id": shot["shot_id"], "status": "generated", "required": required,
                                  "model": provider.spec.model_id, "request_id": record.request_id,
                                  "seconds": seconds})
                    self.log(f"clip {shot['shot_id']} done")
                    break
                except ProviderError as exc:
                    # A required plot action gets bounded retries (each re-checked against the fal cap above).
                    if required and store.retire_failed(f"clip:{shot['shot_id']}:", REQUIRED_MOTION_RETRIES):
                        self.log(f"clip {shot['shot_id']} failed; retrying required motion: {exc}")
                        continue
                    status = "failed_required" if required else "failed_used_still"
                    notes.append({"shot_id": shot["shot_id"], "status": status, "required": required,
                                  "error": str(exc)})
                    self.log(f"clip {shot['shot_id']} FAILED, using animated still: {exc}")
                    break
        return clips, notes

    # ---------------------------------------------------------------- render
    def render(self, shots: list[dict], images: list[Path], clips: dict[int, Path], windows: list[float],
               tl: Timeline, narration: Path, cues: list[Cue], music: Path | None = None,
               room: Path | None = None) -> Path:
        transitions = [transition_for(s, i, i == 0 or scene_index(s) != scene_index(shots[i - 1]))
                       for i, s in enumerate(shots)]
        lengths, offsets = xfade_plan(windows, [d for _, d in transitions])
        seg_dir = self.p("work", "segments", "x").parent
        segments = []
        for i, length in enumerate(lengths):
            dest = seg_dir / f"seg_{i + 1:02}.mp4"
            move = normalize_move(shots[i].get("camera_movement", ""), i)
            shots[i]["render_move"] = move
            shots[i]["render_transition_in"] = transitions[i][0] if i else "fade_from_black"
            if i in clips:
                self.segment_from_clip(images[i], clips[i], dest, length, move)
            else:
                self.segment_from_still(images[i], dest, length, move)
            segments.append(dest)
        leads = sorted(seg_dir.glob("seg_*_lead.mp4"))
        self.scratch(segments + leads, "intermediate render segment")
        starts = cumulative(windows)
        zones = [CAPTION_ZONES.get(s.get("caption_zone", "mid"), CAPTION_ZONES["mid"]) for s in shots]
        caption_list = self.caption_track(cues, tl.total,
                                          zone_at=lambda t: zones[max(0, sum(1 for st in starts if st <= t) - 1)])

        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for s in segments:
            args += ["-i", str(s)]
        n = len(segments)
        args += ["-f", "concat", "-safe", "0", "-i", str(caption_list), "-i", str(narration)]
        if music is not None:
            args += ["-stream_loop", "-1", "-i", str(music)]
        if room is not None:
            args += ["-i", str(room)]
        room_idx = n + 2 + (1 if music is not None else 0)
        graph = [f"[{k}:v]setpts=PTS-STARTPTS,fps={FPS},format=yuv420p[s{k}]" for k in range(n)]
        last = "[s0]"
        for k, offset in enumerate(offsets, start=1):
            name, dur = transitions[k]
            graph.append(f"{last}[s{k}]xfade=transition={name}:duration={dur}:offset={offset:.4f}[x{k}]")
            last = f"[x{k}]"
        total = tl.total
        graph.append(f"{last}{FILM_LOOK},fade=t=in:st=0:d=0.6,fade=t=out:st={total - 1.0:.3f}:d=1.0[base]")
        graph.append(f"[{n}:v]fps={FPS},format=rgba[cap]")
        graph.append("[base][cap]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[v]")
        voice, bed = f"[{n + 1}:a]", ""
        if room is not None:
            # Room tone joins the final mix at its fixed low level; it never feeds the music sidechain.
            graph.append(f"[{room_idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.4f},"
                         f"asetpts=N/SR/TB,afade=t=in:st=0:d=1.5,"
                         f"afade=t=out:st={max(0.0, total - 2.0):.3f}:d=2.0[rt]")
            bed = "[rt]"
        if music is None and room is not None:
            graph.append(f"[{n + 1}:a]aresample=48000,aformat=channel_layouts=stereo,"
                         f"loudnorm=I=-16:TP=-2:LRA=11,aresample=48000[vn]")
            graph.append("[vn][rt]amix=inputs=2:normalize=0:duration=first[vr]")
            voice = "[vr]"
        if music is None:
            graph.append(f"{voice}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
                         f"afade=t=in:st=0:d=0.3,afade=t=out:st={total - 0.8:.3f}:d=0.8[a]")
        else:
            # Voice at -16 LUFS, music bed at -28 LUFS, ducked a further ~10 dB while anyone speaks.
            graph.append(f"[{n + 1}:a]aresample=48000,aformat=channel_layouts=stereo,"
                         f"loudnorm=I=-16:TP=-2:LRA=11,aresample=48000,asplit=2[vo][vsc]")
            graph.append(f"[{n + 2}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.4f},"
                         f"asetpts=N/SR/TB,loudnorm=I=-28:TP=-4:LRA=9,aresample=48000,"
                         f"afade=t=in:st=0:d=2.0,afade=t=out:st={max(0.0, total - 3.0):.3f}:d=3.0[mu]")
            graph.append("[mu][vsc]sidechaincompress=threshold=0.015:ratio=8:attack=20:release=450[duck]")
            graph.append(f"[vo][duck]{bed}amix=inputs={3 if bed else 2}:normalize=0:duration=first,"
                         f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
                         f"afade=t=in:st=0:d=0.3,afade=t=out:st={total - 0.8:.3f}:d=0.8[a]")
        master = self.p("master_9x16.mp4")
        run(*args, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{total:.4f}",
            "-movflags", "+faststart", str(master))
        return master

    def _fit(self) -> str:
        w, h = self.width, self.height
        return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1"

    def segment_from_still(self, image: Path, dest: Path, seconds: float, move: str) -> None:
        w, h = self.width, self.height
        frames = max(2, round(seconds * FPS))
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase:flags=lanczos,crop={w * 2}:{h * 2},"
              f"{zoompan_expr(move, frames, w, h)},setsar=1,format=yuv420p")
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(image), "-vf", vf,
            "-frames:v", str(frames), "-r", str(FPS), "-c:v", "libx264", "-preset", "fast", "-crf", "16",
            "-an", str(dest))

    def segment_from_clip(self, image: Path, clip: Path, dest: Path, seconds: float, move: str) -> None:
        """Clip slowed up to 1.3x; if the shot is longer, a moving still dissolves into the clip."""
        clip_s = probe_seconds(clip)
        frames = max(2, round(seconds * FPS))
        stretch = max(1.0, min(1.3, seconds / clip_s)) if clip_s > 0 else 1.0  # never speed clips up
        played = clip_s * max(stretch, 1.0)
        fit = self._fit()
        if played >= seconds - 0.05:
            vf = (f"setpts={stretch:.4f}*PTS,fps={FPS},{fit},tpad=stop_mode=clone:stop_duration={seconds:.3f},"
                  f"trim=end_frame={frames},format=yuv420p")
            run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip), "-vf", vf, "-an",
                "-c:v", "libx264", "-preset", "fast", "-crf", "16", str(dest))
            return
        xf = 0.5
        lead = seconds - played + xf
        lead_file = dest.with_name(dest.stem + "_lead.mp4")
        self.segment_from_still(image, lead_file, lead, "push_in")
        graph = (f"[1:v]setpts={stretch:.4f}*PTS,fps={FPS},{fit},format=yuv420p[c];"
                 f"[0:v]fps={FPS},format=yuv420p[l];"
                 f"[l][c]xfade=transition=dissolve:duration={xf}:offset={lead - xf:.4f},"
                 f"trim=end_frame={frames},format=yuv420p[v]")
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(lead_file), "-i", str(clip),
            "-filter_complex", graph, "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "16",
            str(dest))

    def caption_track(self, cues: list[Cue], total: float,
                      zone_at: Callable[[float], float] | None = None) -> Path:
        """Transparent PNG per cue + an FFmpeg concat list with exact durations (no libass needed).

        zone_at(t) gives the caption's vertical centre (fraction of height) for the shot on screen at t, so a
        shot can move captions clear of faces, hands or story-critical props (storyboard "caption_zone")."""
        cap_dir = self.p("work", "captions", "x").parent
        w, h = self.width, self.height
        blank = cap_dir / "blank.png"
        Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(blank)
        font = load_font(round(w * 0.074))
        lines = []
        last = blank
        for idx, dur in caption_timeline(cues, total):
            y_frac = zone_at(cues[idx].start) if (zone_at and idx is not None) else CAPTION_ZONES["mid"]
            path = blank if idx is None else draw_caption(cap_dir / f"cue_{idx:03}.png", cues[idx].text, font, w,
                                                          h, y_frac)
            lines += [f"file '{path.resolve().as_posix()}'", f"duration {dur:.4f}"]
            last = path
        lines.append(f"file '{last.resolve().as_posix()}'")  # concat demuxer needs the last file repeated
        listing = cap_dir / "captions.txt"
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.scratch(sorted(cap_dir.glob("*.png")) + [listing], "caption overlay frame")
        return listing

    # ---------------------------------------------------------------- reports
    def finish(self, story, shots, images, clips, motion_notes, audio, tl, windows, cues, master, ledger, est,
               before, after) -> Path:
        limits = self.limits()
        profile = episode_profile(self.width, self.height, limits)
        words = sum(len(s.narration.split()) for s in story.scenes)
        checks = technical_checks(master, profile)
        verdict, detail = duration_verdict(probe_seconds(master), limits)
        if verdict == "warn":
            checks.append(Check(name="duration_target", status="warn", detail=detail))
        checks += caption_checks(self.folder / "captions.srt", words, probe_seconds(master),
                                 final_hold=tl.hold(len(tl.audio_seconds) - 1))
        checks += repeated_visual_checks(images)
        missing = [str(p) for p in images if not p.is_file()]
        checks.append(Check(name="required_media", status="fail" if missing else "pass",
                            detail=f"missing: {missing}" if missing else f"{len(images)} stills, {len(clips)} clips"))
        sync = abs(tl.total - probe_seconds(self.folder / "audio" / "narration.wav"))
        checks.append(Check(name="scene_timing_matches_narration", status="pass" if sync < 0.1 else "fail",
                            detail=f"timeline {tl.total:.2f}s vs narration track, diff {sync:.3f}s"))
        fal = self.fal_spent(ledger)
        credits = self.credits_spent(ledger)
        checks.append(Check(name="budget", status="pass" if fal <= self.max_fal_usd and
                            credits <= self.max_voice_credits else "fail",
                            detail=f"fal ${fal:.3f} of ${self.max_fal_usd:.2f}; ElevenLabs {credits} of "
                                   f"{self.max_voice_credits} credits (estimated)"))
        failed_motion = [n for n in motion_notes if n["status"] == "failed_used_still"]
        if failed_motion:
            checks.append(Check(name="motion_clips", status="warn",
                                detail=f"{len(failed_motion)} clip(s) failed and use animated stills"))
        required = [s["shot_id"] for s in shots if s.get("motion_required")]
        if required:
            got = {n["shot_id"] for n in motion_notes if n["status"] in ("generated", "cached", "dry_run")}
            missing_motion = [sid for sid in required if sid not in got]
            checks.append(Check(name="required_motion", status="fail" if missing_motion else "pass",
                                detail=(f"no motion clip for required action shots {missing_motion}: needs review"
                                        if missing_motion else f"{len(required)} required action clip(s) present; "
                                        "Claude's frame review confirms each action actually happens")))
        passed = all(c.status != "fail" for c in checks)
        quality = {"passed": passed, "dry_run": self.dry_run, "video": str(master),
                   "checks": [c.model_dump() for c in checks],
                   "editorial_review": "Claude Code frame review recorded separately in reports/editorial_review.md",
                   "not_automatically_validated": [
                       "Story quality, emotional pacing and originality (Claude editorial review)",
                       "Character face/wardrobe consistency across shots (text-anchored, not reference-conditioned)",
                       "Caption readability on real phones and platform UI overlap",
                       "Voice acting quality and pronunciation",
                       "Platform AI-disclosure settings at upload time"]}
        write_json(self.p("reports", "quality_report.json"), quality)
        write_json(self.p("qc.json"), {"exports": {"master": {"filename": master.name, "width": self.width,
                                                               "height": self.height,
                                                               "duration_seconds": round(probe_seconds(master), 2),
                                                               "audio_present": True}},
                                        "full_report": "reports/quality_report.json"})
        timing = [{k: v for k, v in a.items() if k != "alignment"} |
                  {"window_start_seconds": round(s, 3), "speech_start_seconds": round(ss, 3),
                   "window_seconds": round(wd, 3)}
                  for a, s, ss, wd in zip(audio, tl.starts, tl.speech_starts, tl.windows)]
        write_json(self.p("audio", "audio_timing.json"), {"gap_seconds": tl.gap, "tail_seconds": tl.tail,
                                                            "total_seconds": round(tl.total, 3),
                                                            "segments": timing})
        for idx, (shot, s, wd, img) in enumerate(zip(shots, cumulative(windows), windows, images)):
            shot["actual_start_seconds"], shot["actual_duration_seconds"] = round(s, 3), round(wd, 3)
            shot["actual_asset"] = {"image": str(img.relative_to(self.folder)),
                                    "clip": str(clips[idx].relative_to(self.folder)) if idx in clips else None,
                                    "media_type": "generated_video" if idx in clips else "generated_still"}
        board_path = self.folder / "storyboard.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        board["shots"], board["actual_total_seconds"] = shots, round(tl.total, 3)
        write_json(board_path, board)
        if self.story_layout:
            final, thumb = None, self.p("thumbnail.png")   # one master, nothing else to keep in sync
        else:
            # Legacy path kept, but as a hard link: byte-identical, one physical copy on disk.
            final, thumb = self.p("final", f"episode_{story.episode_number:02}.mp4"), self.p("final", "thumbnail.png")
            final.unlink(missing_ok=True)
            try:
                os.link(master, final)
            except OSError:
                shutil.copyfile(master, final)
        thumb_src = images[next((i for i, s in enumerate(shots) if s.get("thumbnail")), len(images) * 2 // 3)]
        with Image.open(thumb_src) as im:
            ImageOps.fit(im.convert("RGB"), (self.width, self.height), Image.LANCZOS).save(thumb)
        consumed = (after["used"] - before["used"]) if before and after else None
        write_json(self.p("reports", "generation_report.json"), {
            "dry_run": self.dry_run, "episode": story.title, "scenes": len(story.scenes),
            "shots": len(shots), "images": len(images), "motion_clips": len(clips),
            "motion": motion_notes, "captions": len(cues), "estimate": est,
            "fal_usd_estimated_spend": round(fal, 4),
            "elevenlabs_characters_sent": credits,
            "elevenlabs_credits_measured_from_account_this_run": consumed,
            "elevenlabs_balance_before": before, "elevenlabs_balance_after": after,
            "duration_seconds": round(tl.total, 2), "master": str(master),
            "master_bytes": master.stat().st_size, "final_copy": str(final) if final else None,
            "music": getattr(self, "music_meta", {"source": "none"}),
            "room_tone": getattr(self, "room_tone_meta", {"source": "none"}),
            "asset_provenance": "All images/clips generated for this episode by fal (FLUX 2 Flash, Wan 3.0); "
                                "voices from the ElevenLabs roster. No stock, web or real-person media.",
            "note": "One master for Shorts and TikTok" + ("" if final is None else
                                                          "; final/ is a hard link to master_9x16.mp4") + "."})
        self.update_status("RENDERED_QC_PASSED" if passed else "QC_FAILED", qc_passed=passed)
        return master

    def update_status(self, stage: str, **extra: Any) -> None:
        from datetime import datetime, timezone
        path = self.folder / "status.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.update(stage=stage, updated_utc=datetime.now(timezone.utc).isoformat(),
                    publication_approved=data.get("publication_approved", False), **extra)
        write_json(path, data)


# ============================================================================ ffmpeg / drawing helpers
def run(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(args), check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{args[0]} failed: {exc.stderr[-1500:]}") from exc


def probe_seconds(path: Path) -> float:
    out = run("ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", str(path)).stdout.strip()
    return float(out)


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def draw_caption(path: Path, text: str, font: Any, w: int, h: int, y_frac: float = 0.66) -> Path:
    """Big bold uppercase words, thick outline, centred at y_frac of the height (default 66%, clear of UI)."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    words, lines, line = text.upper().split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) > w * 0.84 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    size = getattr(font, "size", 20)
    stroke = max(2, round(size * 0.09))
    line_h = round(size * 1.15)
    y = round(h * y_frac) - line_h * len(lines) // 2
    for ln in lines:
        x = (w - draw.textlength(ln, font=font)) / 2
        draw.text((x, y), ln, font=font, fill=(255, 255, 255, 255), stroke_width=stroke,
                  stroke_fill=(0, 0, 0, 255))
        y += line_h
    img.save(path)
    return path


# ============================================================================ dry-run fakes (no network)
def fake_image(dest: Path, index: int) -> None:
    img = Image.new("RGB", (STILL_SIZE["width"], STILL_SIZE["height"]))
    draw = ImageDraw.Draw(img)
    for y in range(0, img.height, 8):
        shade = int(30 + 60 * y / img.height)
        draw.rectangle((0, y, img.width, y + 8), fill=(shade // 2 + index * 5 % 80, shade // 2, shade + 40))
    draw.ellipse((150 + index * 23 % 500, 500 + index * 61 % 700, 650 + index * 23 % 500, 1000 + index * 61 % 700),
                 outline=(255, 190, 90), width=14)
    draw.text((120, 200), f"DRY RUN SHOT {index + 1}", fill=(255, 255, 255), font=load_font(70))
    img.save(dest)


def fake_clip(image: Path, dest: Path, seconds: int, w: int, h: int) -> None:
    run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(image), "-t", str(seconds),
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},hue=h=t*20,format=yuv420p",
        "-r", "24", "-c:v", "libx264", "-preset", "ultrafast", str(dest))


def fake_voice(dest: Path, text: str) -> dict:
    """Tone audio sized like real speech, with an evenly spaced fake character alignment."""
    from studio.stories import WORDS_PER_SECOND
    seconds = max(1.5, len(text.split()) / WORDS_PER_SECOND)
    run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=frequency=220:duration={seconds:.3f}", "-af", "volume=0.3", "-c:a", "libmp3lame", str(dest))
    step = seconds / max(1, len(text))
    alignment = {"characters": list(text),
                 "character_start_times_seconds": [i * step for i in range(len(text))],
                 "character_end_times_seconds": [(i + 1) * step for i in range(len(text))]}
    dest.with_suffix(".alignment.json").write_text(json.dumps(alignment), encoding="utf-8")
    return alignment


def dry_run_folder(folder: Path) -> Path:
    """Rehearsal copy so placeholder media can never be mistaken for cached paid assets.

    Story-folder episodes rehearse in <episode>/work/dryrun (listed as scratch); older folders use the
    sibling <episode>__dryrun as before."""
    target = dry_run_target(folder)
    if target.is_symlink():
        raise ValueError(f"Refusing to replace symlinked rehearsal folder {target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in ("story.json", "status.json", "storyboard.json"):
        shutil.copy2(folder / name, target / name)
    if is_story_episode(folder):
        manifest = ScratchManifest(folder)
        manifest.add([target], "offline rehearsal with placeholder media", kind="dir")
        manifest.save()
    return target
