"""Live provider wiring and offline dry-run fakes for the autopilot."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
from typing import Callable

from PIL import Image, ImageDraw

from studio.autopilot import AutopilotConfig, Services
from studio.media import ElevenLabsTTS
from studio.models import (FactReview, FrameReview, ResearchBrief, ScriptSection, ShortScript,
                           ShotPlan, ShotPlans, TopicCandidate, TopicCandidates, VideoScript)
from studio.providers import build_image_generator, build_video_providers, generate_clip
from studio.providers.base import JobRecord, RetryPolicy, VideoRequest, default_http

LICENSE_NOTES = {
    "fal": "Generated via fal.ai; commercial rights depend on the specific model's license. Verify on the model page.",
    "runway": "Generated via Runway API; verify Runway terms for commercial use.",
    "elevenlabs": "ElevenLabs TTS; commercial use depends on your plan and the voice's license.",
}


# =========================================================================== live
def preflight(config: AutopilotConfig, catalog: dict, env: dict | None = None) -> dict:
    """No-cost readiness report for live runs."""
    env = os.environ if env is None else env
    problems, warnings = [], []
    try:
        import anthropic  # noqa: F401
    except ImportError:
        problems.append("Python package 'anthropic' not installed (pip install -r requirements.txt)")
    if not env.get("ANTHROPIC_API_KEY"):
        warnings.append("ANTHROPIC_API_KEY not set; the SDK will try other configured credentials")
    if not env.get("FAL_KEY"):
        problems.append("FAL_KEY not set (images and video)")
    for name in ("ELEVENLABS_API_KEY", config.voice.env_key):
        if not env.get(name):
            problems.append(f"{name} not set")
    if config.voice.usd_per_1k_characters is None:
        problems.append("voice.usd_per_1k_characters missing in config/autopilot.json (needed for budget enforcement)")
    if not config.voice.commercial_license_confirmed:
        warnings.append("voice.commercial_license_confirmed is false; QC will flag every video for license review")
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            problems.append(f"{binary} not found on PATH")
    if shutil.which("ffmpeg"):
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        if " subtitles " not in filters:
            problems.append("ffmpeg lacks libass 'subtitles' filter (needed to burn Short captions)")
    if config.claude.model not in catalog["claude"]["prices_usd_per_mtok"]:
        problems.append(f"No verified price for {config.claude.model} in config/providers.json")
    usable = [k for k in config.video.fallback_order
              if (catalog["video_models"][k]["platform"] == "fal" and env.get("FAL_KEY"))
              or (catalog["video_models"][k]["platform"] == "runway" and env.get("RUNWAYML_API_SECRET"))]
    if not usable:
        warnings.append("No video provider has credentials; motion shots will degrade to animated stills")
    return {"ready": not problems, "problems": problems, "warnings": warnings,
            "video_fallback_chain": usable, "catalog_verified_on": catalog.get("verified_on")}


def build_live_services(config: AutopilotConfig, catalog: dict, env: dict | None = None) -> Services:
    env = os.environ if env is None else env
    report = preflight(config, catalog, env)
    if not report["ready"]:
        raise RuntimeError("Live run not ready:\n- " + "\n- ".join(report["problems"]))
    import anthropic

    from studio.llm import ClaudeStudio

    policy = RetryPolicy(timeout_s=config.video.timeout_minutes * 60)
    http = default_http()
    llm = ClaudeStudio(anthropic.Anthropic(max_retries=4), config.claude.model, catalog["claude"],
                       config.claude.effort, config.channel_brief)
    images = build_image_generator(catalog, config.image_model, policy, http, env)
    providers = build_video_providers(catalog, config.video.fallback_order, http, env)

    def make_clip(req: VideoRequest, dest: Path, store, ledger, should_stop: Callable[[], bool]):
        provider, record = generate_clip(providers, req, http=http, store=store, ledger=ledger, dest=dest,
                                         policy=policy, should_stop=should_stop)
        return provider.spec.platform, provider.spec.model_id, record.request_id

    tts = ElevenLabsTTS(http, env["ELEVENLABS_API_KEY"], env[config.voice.env_key], config.voice, policy)
    return Services(llm=llm, images=images, make_clip=make_clip, tts=tts, dry_run=False,
                    licenses=LICENSE_NOTES)


# =========================================================================== dry-run fakes
DRY_TOPICS = [
    ("How tide mills powered medieval coasts", "the engineering of harnessing two tides a day"),
    ("The quiet science of desert fog harvesting", "nets that drink clouds in arid towns"),
    ("Why glaciers sing before they calve", "acoustic clues scientists record in the ice"),
    ("Inside the world's oldest seed vault ideas", "how communities protect crop diversity"),
    ("Lighthouse lenses that bent light for ships", "the Fresnel design that saved sailors"),
    ("The surprising navigation skills of eels", "magnetic maps across an entire ocean"),
    ("Volcanic glass and the first sharp tools", "obsidian trade routes in prehistory"),
    ("How beavers redesign entire valleys", "dams that store water and rebuild wetlands"),
    ("The mathematics hidden in honeycombs", "why hexagons use the least wax"),
    ("Coral spawning under a full moon", "the synchronized night that renews reefs"),
]

VOCAB = ("river stone light water careful early people built measured patient tide wheel coast "
         "season record wind lens ocean signal craft village story evidence century surprising "
         "simple design energy morning shape slow change").split()


def _sentences(seed: str, words: int) -> str:
    rng = random.Random(seed)
    out, total = [], 0
    while total < words:
        n = min(12, words - total)
        n = max(n, 3)
        sentence = " ".join(rng.choice(VOCAB) for _ in range(n))
        out.append(sentence[0].upper() + sentence[1:] + ".")
        total += n
    return " ".join(out)


class FakeLLM:
    """Deterministic, zero-cost stand-in for ClaudeStudio."""

    def discover_topics(self, ledger, niche, audience, avoid, count=6):
        start = len(avoid)
        picks = [DRY_TOPICS[(start + i) % len(DRY_TOPICS)] for i in range(count)]
        return TopicCandidates(candidates=[TopicCandidate(title=t, angle=a, audience_value="dry run",
                                                          why_now="dry run") for t, a in picks])

    def research(self, ledger, title, angle, max_searches):
        slug = hashlib.sha1(title.encode()).hexdigest()[:8]
        from studio.models import Claim
        claims = [Claim(claim_id=f"c{i:02}", statement=f"Dry-run claim {i} about {title}.",
                        source_urls=[f"https://example.org/dryrun/{slug}/{i}"], confidence="high")
                  for i in range(1, 13)]
        return (ResearchBrief(summary=f"Dry-run research for {title}", claims=claims, open_questions=[]),
                {u for c in claims for u in c.source_urls})

    def write_script(self, ledger, title, angle, brief, words, feedback=""):
        target = (words[0] + words[1]) // 2
        per = target // 6
        sections = [ScriptSection(heading=f"Part {i + 1}", narration=_sentences(f"{title}{i}", per),
                                  claim_ids=[f"c{(i % 12) + 1:02}"]) for i in range(6)]
        return VideoScript(title=title, hook=sections[0].narration.split(".")[0], sections=sections,
                           youtube_description=f"Dry-run description for {title}.", tags=["dry-run"],
                           thumbnail_text="Dry run preview")

    def write_short(self, ledger, script, brief, words, feedback=""):
        return ShortScript(title=f"{script.title} (Short)", narration=_sentences(script.title + "short",
                           (words[0] + words[1]) // 2), claim_ids=["c01"], description="Dry-run Short.",
                           tags=["dry-run"])

    def review_facts(self, ledger, script, short, brief):
        return FactReview(verdict="pass", issues=[], notes="dry run")

    def plan_shots(self, ledger, title, style, aspect, chunks, motion_slots):
        cams = ["zoom_in", "pan_right", "zoom_out", "pan_left", "push_slow"]
        return ShotPlans(shots=[ShotPlan(shot_id=sid, visual_prompt=f"Dry-run visual for: {text[:60]}",
                                         wants_motion=(i % 7 == 1), camera=cams[i % len(cams)],
                                         motion_prompt="gentle drifting motion")
                                for i, (sid, text) in enumerate(chunks)])

    def review_frames(self, ledger, title, frames):
        return FrameReview(visual_quality_score=8, style_consistency_score=8, issues=[], verdict="pass")


class FakeImages:
    spec = {"model_id": "dryrun/procedural-placeholder"}

    def generate(self, *, key, prompt, size, dest, store, ledger, should_stop=lambda: False, sleep=None):
        if not dest.is_file():
            rng = random.Random(key)
            w, h = size["width"] // 2, size["height"] // 2
            top = tuple(rng.randrange(20, 120) for _ in range(3))
            bottom = tuple(rng.randrange(120, 230) for _ in range(3))
            img = Image.new("RGB", (w, h))
            draw = ImageDraw.Draw(img)
            for y in range(h):
                t = y / h
                draw.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
            for _ in range(rng.randrange(3, 7)):
                x, y, r = rng.randrange(w), rng.randrange(h), rng.randrange(h // 10, h // 3)
                draw.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rng.randrange(256) for _ in range(3)))
            dest.parent.mkdir(parents=True, exist_ok=True)
            img.save(dest)
        return JobRecord(key=key, provider="dryrun", model_id=self.spec["model_id"],
                         request_id="dryrun-" + hashlib.sha1(key.encode()).hexdigest()[:10],
                         status="downloaded", output_path=str(dest))


def fake_make_clip(req: VideoRequest, dest: Path, store, ledger, should_stop):
    seconds = min(10, math.ceil(req.seconds_needed))  # mimic provider max length -> exercises hold-frame
    w, h = (1280, 720) if req.aspect_ratio == "16:9" else (720, 1280)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(req.image_path),
                    "-t", str(seconds), "-vf",
                    f"scale={int(w * 1.15)}:{int(h * 1.15)},crop={w}:{h}:x='(iw-ow)*t/{seconds}':y='(ih-oh)/2',fps=24,format=yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", str(dest)], check=True, capture_output=True)
    return "dryrun", "dryrun/pan-clip", "dryrun-" + req.shot_id


class FakeTTS:
    """Tone audio at ~2.5 words/s with evenly spread character alignment."""

    def synthesize(self, *, text, dest_audio: Path, previous_text, next_text, ledger):
        import json
        align_path = dest_audio.with_suffix(".alignment.json")
        if dest_audio.is_file() and align_path.is_file():
            return dest_audio, json.loads(align_path.read_text())
        seconds = max(1.0, len(text.split()) / 2.5)
        dest_audio.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency=180:sample_rate=44100:duration={seconds:.3f}", "-af", "volume=0.25",
                        "-c:a", "libmp3lame", "-q:a", "5", str(dest_audio)], check=True, capture_output=True)
        n = len(text)
        alignment = {"characters": list(text),
                     "character_start_times_seconds": [round(i * seconds / n, 3) for i in range(n)],
                     "character_end_times_seconds": [round((i + 1) * seconds / n, 3) for i in range(n)]}
        align_path.write_text(json.dumps(alignment))
        return dest_audio, alignment


def build_dryrun_services() -> Services:
    return Services(llm=FakeLLM(), images=FakeImages(), make_clip=fake_make_clip, tts=FakeTTS(),
                    dry_run=True, licenses={"dryrun": "offline placeholder, not for publication"})
