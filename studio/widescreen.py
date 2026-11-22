"""Free 16:9 re-render of an approved 9:16 story episode from its retained sources.

No paid calls. Reuses the accepted stills and clips, the exact shot timing, moves and
transitions recorded in storyboard.json, and the master's own final audio mix. Each shot is
cropped from the portrait source at a per-shot height (faces sit high, inserts and low-action
shots lower), and captions are redrawn small in the lower third for a landscape screen.
"""
from __future__ import annotations

import copy
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from studio.captions import parse_srt
from studio.episode_production import FPS, Production, Timeline, draw_caption, load_font, zoompan_expr

WIDTH, HEIGHT = 1920, 1080
CAPTION_Y = 0.87          # lower third, inside the TV/web safe area
CAPTION_FONT = 0.058      # of the frame height
# Where to centre the 16:9 band in the tall source: 0 = top, 1 = bottom (fraction of the spare height).
FOCUS_BY_SIZE = {"insert": 0.45, "extreme_close": 0.42, "close": 0.32, "medium": 0.33,
                 "over_shoulder": 0.35, "two_shot": 0.35, "wide": 0.40}
LOW_CONTENT_SHIFT = 0.20  # storyboard caption_zone "high" = faces/hands/prop low in the frame


def shot_focus(shot: dict) -> float:
    f = FOCUS_BY_SIZE.get(shot.get("shot_size", ""), 0.35)
    if shot.get("caption_zone") == "high":
        f += LOW_CONTENT_SHIFT
    return max(0.0, min(0.75, f))


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@dataclass
class WideRender(Production):
    focus: list[float] = field(default_factory=list)
    _cur: float = 0.35

    def _set_focus(self, dest: Path) -> None:
        idx = int(dest.stem.split("_")[1]) - 1          # seg_07.mp4 / seg_07_lead.mp4 -> 6
        self._cur = self.focus[idx]

    def _crop(self, w: int, h: int) -> str:
        return f"crop={w}:{h}:(iw-{w})/2:(ih-{h})*{self._cur:.3f}"

    def _fit(self) -> str:
        w, h = self.width, self.height
        return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,{self._crop(w, h)},setsar=1"

    def segment_from_still(self, image: Path, dest: Path, seconds: float, move: str) -> None:
        self._set_focus(dest)
        w, h = self.width, self.height
        frames = max(2, round(seconds * FPS))
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase:flags=lanczos,{self._crop(w * 2, h * 2)},"
              f"{zoompan_expr(move, frames, w, h)},setsar=1,format=yuv420p")
        _ffmpeg("-i", str(image), "-vf", vf, "-frames:v", str(frames), "-r", str(FPS), "-c:v", "libx264",
                "-preset", "fast", "-crf", "16", "-an", str(dest))

    def segment_from_clip(self, image: Path, clip: Path, dest: Path, seconds: float, move: str) -> None:
        self._set_focus(dest)
        super().segment_from_clip(image, clip, dest, seconds, move)

    def caption_track(self, cues, total, zone_at=None) -> Path:
        cap_dir = self.p("work", "captions", "x").parent
        w, h = self.width, self.height
        from PIL import Image
        from studio.episode_production import caption_timeline
        blank = cap_dir / "blank.png"
        Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(blank)
        font = load_font(round(h * CAPTION_FONT))
        lines, last = [], blank
        for idx, dur in caption_timeline(cues, total):
            path = blank if idx is None else draw_caption(cap_dir / f"cue_{idx:03}.png", cues[idx].text, font,
                                                          w, h, CAPTION_Y)
            lines += [f"file '{path.resolve().as_posix()}'", f"duration {dur:.4f}"]
            last = path
        lines.append(f"file '{last.resolve().as_posix()}'")
        listing = cap_dir / "captions.txt"
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return listing


def render_episode(root: Path, episode: Path, out: Path, work: Path) -> Path:
    """Render one approved episode folder to a 1920x1080 MP4 at `out`."""
    board = json.loads((episode / "storyboard.json").read_text(encoding="utf-8"))
    shots = copy.deepcopy(board["shots"])
    missing = [s["shot_id"] for s in shots if "actual_duration_seconds" not in s or "actual_asset" not in s]
    if missing:
        raise ValueError(f"{episode}: storyboard has no recorded render timing for {missing}")
    master = episode / "master_9x16.mp4"
    images = [episode / s["actual_asset"]["image"] for s in shots]
    clips = {i: episode / s["actual_asset"]["clip"] for i, s in enumerate(shots) if s["actual_asset"].get("clip")}
    for p in [master, *images, *clips.values()]:
        if not p.is_file():
            raise FileNotFoundError(f"Retained source missing: {p}")
    windows = [float(s["actual_duration_seconds"]) for s in shots]
    total = sum(windows)
    cues = parse_srt(episode / "captions.srt")

    work.mkdir(parents=True, exist_ok=True)
    narration = work / "master_audio.wav"
    _ffmpeg("-i", str(master), "-vn", "-c:a", "pcm_s16le", str(narration))
    r = WideRender(root=root, folder=work, max_fal_usd=0.0, max_voice_credits=0, width=WIDTH, height=HEIGHT,
                   focus=[shot_focus(s) for s in shots], log=lambda *_: None)
    video = r.render(shots, images, clips, windows, Timeline([total], gap=0.0, tail=0.0), narration, cues)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Keep the approved master's exact audio mix (voices, score, room tone).
    _ffmpeg("-i", str(video), "-i", str(master), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
            "-shortest", "-movflags", "+faststart", str(out))
    return out


def render_story(root: Path, slug: str, out: Path, keep_episodes: Path | None = None,
                 log=print) -> tuple[Path, list[Path]]:
    """All approved episodes of a story-folder series -> one 16:9 MP4 (episodes joined in order)."""
    episodes = sorted(p for p in (root / "series" / slug / "episodes").iterdir()
                      if p.is_dir() and (p / "release.json").is_file())
    if not episodes:
        raise ValueError(f"No approved episodes in series/{slug}/episodes")
    with tempfile.TemporaryDirectory(prefix="widescreen-") as tmp:
        tmp_dir = Path(tmp)
        dest_dir = keep_episodes or tmp_dir
        parts = []
        for ep in episodes:
            log(f"rendering episode {ep.name} in 16:9 ...")
            parts.append(render_episode(root, ep, dest_dir / f"episode{ep.name}_16x9.mp4", tmp_dir / f"work{ep.name}"))
        listing = tmp_dir / "join.txt"
        listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        partial = out.with_name("." + out.stem + ".partial.mp4")   # never leave a half-written file at `out`
        _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart",
                str(partial))
        partial.replace(out)
        return out, parts if keep_episodes else []
