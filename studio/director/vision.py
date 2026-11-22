"""Free local checks on rendered shots: frames, motion (freeze detection), faces and identity drift.

Faces and similarity use tools/bin/vision_faces (Apple Vision, compiled on first use from
tools/vision_faces.swift). If Swift is unavailable these checks report "unavailable" and the
editorial frame review still applies.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = ROOT / "tools" / "vision_faces.swift"
BINARY = ROOT / "tools" / "bin" / "vision_faces"


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), check=check, capture_output=True, text=True)


def probe(path: Path) -> dict:
    out = _run("ffprobe", "-v", "error", "-show_entries",
               "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
               "-of", "json", str(path)).stdout
    return json.loads(out)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def extract_frames(video: Path, dest_dir: Path, times: list[float]) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, t in enumerate(times):
        out = dest_dir / f"{video.stem}_f{i}.png"
        _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", str(out))
        if out.is_file():
            frames.append(out)
    return frames


def frozen_fraction(video: Path, noise: float = 0.003, min_still: float = 0.5) -> float:
    """Share of the clip with no visible change (freezedetect). A slideshow-like shot scores high."""
    total = duration(video)
    err = _run("ffmpeg", "-hide_banner", "-i", str(video), "-vf",
               f"freezedetect=n={noise}:d={min_still}", "-map", "0:v:0", "-f", "null", "-", check=False).stderr
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", err)]
    frozen = 0.0
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else total
        frozen += max(0.0, e - s)
    return min(1.0, frozen / total) if total > 0 else 1.0


def _binary() -> Path | None:
    if BINARY.is_file() and BINARY.stat().st_mtime >= SWIFT_SRC.stat().st_mtime:
        return BINARY
    if shutil.which("swiftc") is None:
        return None
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    r = _run("swiftc", "-O", str(SWIFT_SRC), "-o", str(BINARY), check=False)
    return BINARY if r.returncode == 0 else None


def available() -> bool:
    return _binary() is not None


def faces(images: list[Path]) -> dict[str, list[list[float]]]:
    """image path -> face boxes [x, y, w, h] (0-1, top-left origin). {} if Vision is unavailable."""
    binary = _binary()
    if binary is None or not images:
        return {}
    data = json.loads(_run(str(binary), "faces", *map(str, images)).stdout)
    return {d["path"]: d["faces"] for d in data}


def crop_face(image: Path, box: list[float], dest: Path, margin: float = 0.35) -> Path:
    with Image.open(image) as img:
        w, h = img.size
        x, y, bw, bh = box
        mx, my = bw * margin, bh * margin
        crop = (max(0, int((x - mx) * w)), max(0, int((y - my) * h)),
                min(w, int((x + bw + mx) * w)), min(h, int((y + bh + my) * h)))
        img.crop(crop).convert("RGB").save(dest)
    return dest


def distance(a: Path, b: Path) -> float | None:
    binary = _binary()
    if binary is None:
        return None
    r = _run(str(binary), "distance", str(a), str(b), check=False)
    try:
        return float(json.loads(r.stdout)["distance"])
    except (ValueError, KeyError):
        return None
