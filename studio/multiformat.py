"""Render one clean 9:16 MP4 master for both TikTok and YouTube Shorts.

The source media is generated once. Neither platform needs a second encode.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True)
class VideoFormat:
    name: str
    width: int
    height: int
    filename: str


FULL_HD = (VideoFormat("master", 1080, 1920, "master_9x16.mp4"),)
PREVIEW = (VideoFormat("master", 360, 640, "master_9x16.mp4"),)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=True)


def seconds_of(path: Path) -> float:
    result = run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path))
    return float(result.stdout.strip())


def dimensions_of(path: Path) -> tuple[int, int]:
    data = json.loads(run("ffprobe", "-v", "quiet", "-print_format", "json",
                          "-show_streams", str(path)).stdout)
    video = next(s for s in data["streams"] if s.get("codec_type") == "video")
    return int(video["width"]), int(video["height"])


def _render_one(folder: Path, images: list[Path], narration: Path,
                durations: list[float], spec: VideoFormat) -> Path:
    # Do not share cached clips across aspect ratios: they have different framing.
    clips_dir = folder / "clips" / f"{spec.width}x{spec.height}"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    for index, (img, duration) in enumerate(zip(images, durations, strict=True), start=1):
        target = clips_dir / f"scene_{index:02}.mp4"
        # Re-render locally, so edits to source images and narration are picked up.
        # Scale to fill and center crop only when source aspect differs from 9:16.
        video_filter = (f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=increase,"
                        f"crop={spec.width}:{spec.height},setsar=1,fps=24,format=yuv420p")
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1",
            "-i", str(img), "-t", f"{duration:.5f}", "-vf", video_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-an", str(target))
        clips.append(target)

    # The source paths are local and never derived from untrusted shell input.
    concat = clips_dir / "concat.txt"
    concat.write_text("".join("file '" + p.resolve().as_posix().replace("'", "'\\''") + "'\n"
                              for p in clips), encoding="utf-8")
    output = folder / spec.filename
    run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-i", str(narration), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-t", f"{sum(durations):.5f}", "-movflags", "+faststart", str(output))
    if dimensions_of(output) != (spec.width, spec.height):
        raise RuntimeError(f"Unexpected export dimensions for {output}")
    return output


def export_all(folder: Path, scene_count: int, narration: Path,
               scene_word_counts: list[int], scene_durations: list[float] | None = None,
               preview: bool = False) -> dict[str, str]:
    """Create one clean vertical master using cached assets with zero provider calls."""
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            raise RuntimeError(f"Install {binary} and add it to PATH")
    images = [folder / "images" / f"scene_{i:02}.png" for i in range(1, scene_count + 1)]
    missing = [str(path) for path in images if not path.is_file()]
    if missing or not narration.is_file():
        raise FileNotFoundError(f"Missing source media. Images: {missing}; narration: {narration}")
    if scene_count != len(scene_word_counts) or not scene_count:
        raise ValueError("Word count must be provided for every scene")
    if scene_durations is not None and (len(scene_durations) != scene_count or any(d <= 0 for d in scene_durations)):
        raise ValueError("Scene narration durations must match the scene count and be positive")
    # Match exact narration length even when TTS encoders add padding to segments.
    total_seconds = seconds_of(narration)
    weights = scene_durations if scene_durations is not None else scene_word_counts
    if sum(weights) <= 0:
        raise ValueError("No valid scene timing weights")
    durations = [total_seconds * w / sum(weights) for w in weights]

    specs = PREVIEW if preview else FULL_HD
    exports = {spec.name: str(_render_one(folder, images, narration, durations, spec)) for spec in specs}
    return exports


def quality_report(exports: dict[str, str]) -> dict:
    expected = {"master": (9, 16)}
    reports = {}
    for key, path_str in exports.items():
        path = Path(path_str)
        width, height = dimensions_of(path)
        a, b = expected[key]
        if abs(width * b - height * a) > max(width, height) * 0.01:
            raise RuntimeError(f"Wrong aspect ratio: {path}")
        data = json.loads(run("ffprobe", "-v", "quiet", "-print_format", "json",
                              "-show_streams", "-show_format", str(path)).stdout)
        if not any(s.get("codec_type") == "audio" for s in data["streams"]):
            raise RuntimeError(f"No audio stream: {path}")
        duration = round(float(data["format"]["duration"]), 2)
        reports[key] = {"filename": path.name, "width": width, "height": height,
                        "duration_seconds": duration, "audio_present": True,
                        "youtube_shorts_duration_compatible": duration <= 180,
                        "tiktok_creator_rewards_duration_candidate": duration > 60,
                        "needs_human_review": True}
    return {"exports": reports,
            "warning": "Platform eligibility also depends on content, accounts and rights; review subtitles, audio, visual continuity and video quality manually."}
