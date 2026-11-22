"""Local FFmpeg editing for the autopilot: animated stills, motion clips, transitions, audio mix,
captions and final H.264/AAC encode. Zero provider calls happen here.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont

from studio.models import RenderProfile, Storyboard

FADE_S = 0.35
SECTION_GAP_S = 0.6


class RenderError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            raise RenderError(f"{binary} not found on PATH. Install FFmpeg (macOS: `brew install ffmpeg`).")


def ff(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise RenderError(f"ffmpeg failed: {exc.stderr.strip()[-800:]}") from exc


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
                          "-show_format", str(path)], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def duration_of(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def has_subtitles_filter() -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return " subtitles " in out.stdout


# --------------------------------------------------------------------------- narration timeline
def build_narration(section_audio: list[Path], work: Path) -> tuple[Path, list[float], list[float]]:
    """Decode sections to 48 kHz mono WAV with a short pause after each; concat losslessly.

    Returns (narration.wav, section start offsets, section lengths incl. trailing pause).
    """
    work.mkdir(parents=True, exist_ok=True)
    wavs, lengths = [], []
    for i, src in enumerate(section_audio):
        dest = work / f"section_{i:02}.wav"
        pad = SECTION_GAP_S if i < len(section_audio) - 1 else 0.8
        ff("-i", str(src), "-af", f"apad=pad_dur={pad}", "-ar", "48000", "-ac", "1",
           "-c:a", "pcm_s16le", str(dest))
        wavs.append(dest)
        lengths.append(duration_of(dest))
    concat = work / "narration_concat.txt"
    concat.write_text("".join(f"file '{p.name}'\n" for p in wavs), encoding="utf-8")
    narration = work / "narration.wav"
    ff("-f", "concat", "-safe", "0", "-i", concat.name, "-c", "copy", narration.name, cwd=work)
    starts, t = [], 0.0
    for length in lengths:
        starts.append(t)
        t += length
    return narration, starts, lengths


def assign_shot_timing(board: Storyboard, alignments: list[dict], starts: list[float],
                       lengths: list[float]) -> Storyboard:
    """Cut on the exact moment each shot's first word is spoken (character alignment)."""
    from studio.captions import char_offset_times

    shots = [s.model_copy() for s in board.shots]
    for sec in range(len(starts)):
        sec_shots = [s for s in shots if s.section_index == sec]
        if not sec_shots:
            raise RenderError(f"Section {sec} has no shots")
        char_index, local_starts = 0, []
        for j, shot in enumerate(sec_shots):
            local_starts.append(0.0 if j == 0 else char_offset_times(alignments[sec], char_index))
            char_index += len(shot.narration) + 1  # TTS text joins shot narration with one space
        for j, shot in enumerate(sec_shots):
            end = local_starts[j + 1] if j + 1 < len(sec_shots) else lengths[sec]
            shot.start_seconds = round(starts[sec] + local_starts[j], 3)
            shot.duration_seconds = round(max(0.8, end - local_starts[j]), 3)
    total = round(sum(lengths), 3)
    # Absorb rounding drift into the final shot so video length == audio length.
    drift = total - sum(s.duration_seconds or 0 for s in shots)
    shots[-1].duration_seconds = round((shots[-1].duration_seconds or 0) + drift, 3)
    return board.model_copy(update={"shots": shots, "total_seconds": total})


# --------------------------------------------------------------------------- segments
def _zoompan(camera: str, frames: int, w: int, h: int, fps: int) -> str:
    n = max(1, frames - 1)
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    z, x, y = {
        "zoom_in": (f"1+0.12*on/{n}", center_x, center_y),
        "zoom_out": (f"1.12-0.12*on/{n}", center_x, center_y),
        "pan_left": ("1.12", f"(iw-iw/zoom)*(1-on/{n})", center_y),
        "pan_right": ("1.12", f"(iw-iw/zoom)*on/{n}", center_y),
        "push_slow": (f"1+0.06*on/{n}", center_x, center_y),
    }[camera]
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps}"


def _fades(seconds: float, fade_in: bool, fade_out: bool) -> str:
    parts = []
    if fade_in:
        parts.append(f"fade=t=in:st=0:d={FADE_S}")
    if fade_out:
        parts.append(f"fade=t=out:st={max(0.0, seconds - FADE_S):.3f}:d={FADE_S}")
    return ("," + ",".join(parts)) if parts else ""


def render_segment(*, source: Path, is_clip: bool, seconds: float, camera: str, profile: RenderProfile,
                   dest: Path, fade_in: bool, fade_out: bool) -> Path:
    w, h, fps = profile.width, profile.height, profile.fps
    frames = max(1, round(seconds * fps))
    if is_clip:
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps},setsar=1,"
              f"tpad=stop_mode=clone:stop_duration={seconds:.3f},format=yuv420p"
              + _fades(seconds, fade_in, fade_out))
        ff("-i", str(source), "-vf", vf, "-frames:v", str(frames), "-an", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "16", "-r", str(fps), str(dest))
    else:
        big_w, big_h = (round(w * 1.5) // 2) * 2, (round(h * 1.5) // 2) * 2
        vf = (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
              + _zoompan(camera, frames, w, h, fps) + ",setsar=1,format=yuv420p"
              + _fades(seconds, fade_in, fade_out))
        ff("-i", str(source), "-vf", vf, "-frames:v", str(frames), "-an", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "16", "-r", str(fps), str(dest))
    return dest


# --------------------------------------------------------------------------- audio mix
def mix_audio(narration: Path, total: float, dest: Path, music: Path | None, music_db: float) -> Path:
    """Narration-led mix; music is ducked under speech; loudness normalised to -14 LUFS."""
    norm = "loudnorm=I=-14:TP=-1.5:LRA=11"
    if music is None:
        ff("-i", str(narration), "-af", f"aformat=channel_layouts=stereo,{norm}", "-ar", "48000",
           "-t", f"{total:.3f}", "-c:a", "aac", "-b:a", "192k", str(dest))
        return dest
    graph = (
        f"[1:a]volume={music_db}dB,aformat=sample_rates=48000:channel_layouts=stereo[m];"
        "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[n1][n2];"
        "[m][n1]sidechaincompress=threshold=0.04:ratio=10:attack=15:release=350[duck];"
        f"[n2][duck]amix=inputs=2:duration=first:normalize=0,"
        f"afade=t=out:st={max(0.0, total - 2.0):.3f}:d=2,{norm}[out]"
    )
    ff("-i", str(narration), "-stream_loop", "-1", "-i", str(music), "-filter_complex", graph,
       "-map", "[out]", "-ar", "48000", "-t", f"{total:.3f}", "-c:a", "aac", "-b:a", "192k", str(dest))
    return dest


# --------------------------------------------------------------------------- full render
def render_video(*, folder: Path, board: Storyboard, sources: dict[str, tuple[Path, bool]],
                 narration: Path, profile: RenderProfile, music: Path | None, music_db: float,
                 captions_ass: Path | None, dest: Path) -> Path:
    """sources maps shot_id -> (file, is_clip). Writes dest atomically; never overwrites a final file
    from another video because every video has its own folder."""
    require_ffmpeg()
    work = folder / "work" / profile.name
    seg_dir = work / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    total = float(board.total_seconds or 0)
    if total <= 0:
        raise RenderError("Storyboard has no timing; run assign_shot_timing first")
    segments: list[Path] = []
    shots = board.shots
    for i, shot in enumerate(shots):
        src, is_clip = sources[shot.shot_id]
        first_in_section = i == 0 or shots[i - 1].section_index != shot.section_index
        last_in_section = i == len(shots) - 1 or shots[i + 1].section_index != shot.section_index
        seg = seg_dir / f"{i:03}_{shot.shot_id}.mp4"
        render_segment(source=src, is_clip=is_clip, seconds=float(shot.duration_seconds or 1),
                       camera=shot.camera, profile=profile, dest=seg,
                       fade_in=first_in_section and i > 0, fade_out=last_in_section)
        segments.append(seg)
    concat = work / "segments.txt"
    concat.write_text("".join(f"file 'segments/{p.name}'\n" for p in segments), encoding="utf-8")
    video_only = work / "video_only.mp4"
    ff("-f", "concat", "-safe", "0", "-i", concat.name, "-c", "copy", video_only.name, cwd=work)
    audio = mix_audio(narration, total, work / "mix.m4a", music, music_db)

    tmp = dest.with_name(dest.stem + ".partial.mp4")
    if captions_ass is not None:
        if not has_subtitles_filter():
            raise RenderError("This FFmpeg build lacks libass (subtitles filter); cannot burn captions")
        local_ass = work / "burn.ass"
        shutil.copyfile(captions_ass, local_ass)
        ff("-i", video_only.name, "-i", audio.name, "-map", "0:v:0", "-map", "1:a:0",
           "-vf", "subtitles=burn.ass", "-c:v", "libx264", "-preset", "medium", "-crf", str(profile.crf),
           "-pix_fmt", "yuv420p", "-c:a", "copy", "-t", f"{total:.3f}", "-movflags", "+faststart",
           str(tmp.resolve()), cwd=work)
    else:
        ff("-i", video_only.name, "-i", audio.name, "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "libx264", "-preset", "medium", "-crf", str(profile.crf), "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-t", f"{total:.3f}", "-movflags", "+faststart", str(tmp.resolve()), cwd=work)
    tmp.replace(dest)
    return dest


def extract_frames(video: Path, count: int, dest_dir: Path) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = duration_of(video)
    frames = []
    for i in range(count):
        t = total * (i + 0.5) / count
        out = dest_dir / f"frame_{i:02}.jpg"
        ff("-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out))
        frames.append(out)
    return frames


# --------------------------------------------------------------------------- thumbnail
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def compose_thumbnail(background: Path, text: str, dest: Path, size: tuple[int, int] = (1280, 720)) -> Path:
    """1280x720 thumbnail: original background plus a short, honest text overlay."""
    with Image.open(background) as img:
        img = img.convert("RGB")
        scale = max(size[0] / img.width, size[1] / img.height)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        left, top = (img.width - size[0]) // 2, (img.height - size[1]) // 2
        img = img.crop((left, top, left + size[0], top + size[1]))
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, int(size[1] * 0.62), size[0], size[1]), fill=(0, 0, 0, 150))
    words = text.upper().split()[:5]
    font = _font(int(size[1] * 0.13))
    line = " ".join(words)
    while draw.textlength(line, font=font) > size[0] * 0.92 and font.size > 24:
        font = _font(font.size - 4)
    tw = draw.textlength(line, font=font)
    draw.text(((size[0] - tw) / 2, size[1] * 0.68), line, font=font, fill=(255, 255, 255, 255),
              stroke_width=max(2, font.size // 18), stroke_fill=(0, 0, 0, 255))
    Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB").save(dest, format="PNG")
    return dest
