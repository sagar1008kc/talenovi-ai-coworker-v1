"""FFmpeg assembly: 9:16 episode master, face-aware 16:9 episode cut, and 8-10 minute 16:9 compilations.

Every shot comes from a motion video (lip-synced where required). There is no still-image path:
`episode_master` refuses a shot without a video. Audio is a multi-track mix: dialogue (exact shot
timing), ambience bed, SFX cues and music, with music and ambience sidechain-ducked under dialogue and
the result loudness-normalized (two targets: stems, then master).
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw

from studio.director import vision
from studio.director.schema import Shot, Shotlist
from studio.episode_production import ROOM_TONES, draw_caption, load_font

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
CAPTION_Y = {"mid": 0.66, "high": 0.30}
FOCUS_BY_FRAMING = {"insert": 0.50, "wide": 0.45}


def ff(*args: str) -> None:
    r = subprocess.run([*FF, *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-1500:]}")


def shot_video(episode_dir: Path, shot: Shot) -> Path:
    cur = shot.assets.get("current", {})
    if not cur.get("video"):
        raise ValueError(f"{shot.shot_id}: no motion video; the director never fills a beat with a still")
    return episode_dir / cur["video"]


def fades(sl: Shotlist, i: int) -> str:
    s, out = sl.shots[i], []
    if s.transition_in in ("fade_in", "fadeblack") or i == 0:
        out.append(f"fade=t=in:st=0:d={0.6 if s.transition_in == 'fadeblack' else 0.4}")
    nxt = sl.shots[i + 1] if i + 1 < len(sl.shots) else None
    if s.transition_in == "fade_out" or (nxt and nxt.transition_in == "fadeblack") or nxt is None:
        d = 0.6 if nxt is None or nxt.transition_in == "fadeblack" else 0.4
        out.append(f"fade=t=out:st={max(0.0, s.duration_sec - d):.3f}:d={d}")
    return ("," + ",".join(out)) if out else ""


# ======================================================================== 9:16 picture
def normalize_segment(src: Path, dest: Path, shot: Shot, w: int, h: int, fps: int, extra_vf: str,
                      caption: Path | None, cap_window: tuple[float, float] | None, crf: int) -> Path:
    vf = (f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1,"
          f"tpad=stop_mode=clone:stop_duration=1,trim=duration={shot.duration_sec:.3f},setpts=PTS-STARTPTS"
          f"{extra_vf},format=yuv420p")
    if caption is not None and cap_window:
        a, b = cap_window
        graph = f"[0:v]{vf}[b];[1:v]format=rgba[c];[b][c]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})',format=yuv420p[v]"
        ff("-i", str(src), "-loop", "1", "-i", str(caption), "-filter_complex", graph, "-map", "[v]", "-an",
           "-t", f"{shot.duration_sec:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-r", str(fps), str(dest))
    else:
        ff("-i", str(src), "-vf", vf, "-an", "-t", f"{shot.duration_sec:.3f}", "-c:v", "libx264", "-preset",
           "medium", "-crf", str(crf), "-r", str(fps), str(dest))
    return dest


def caption_window(shot: Shot, cfg: dict) -> tuple[float, float] | None:
    cur = shot.assets.get("current", {})
    voice = cur.get("voice")
    if not voice:
        return None
    sync = cur.get("sync") or {}
    if sync.get("ok"):                       # captions follow the performance the mouths actually match
        return max(0.0, sync["speech_start"] - 0.05), min(shot.duration_sec, sync["speech_end"] + 0.2)
    lead = cfg["episode_format"]["dialogue_lead_seconds"]
    return lead, min(shot.duration_sec, lead + voice["seconds"] + 0.15)


def concat(parts: list[Path], dest: Path, work: Path) -> Path:
    listing = work / f"{dest.stem}_concat.txt"
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dest))
    return dest


# ======================================================================== audio
def dialogue_track(episode_dir: Path, sl: Shotlist, work: Path) -> Path:
    """One mono 48 kHz WAV: each shot's padded dialogue (or silence) in shot order: exact sync."""
    parts = []
    for s in sl.shots:
        cur = s.assets.get("current", {})
        part = work / f"dlg_{s.shot_id}.wav"
        if cur.get("dialogue_wav"):
            ff("-i", str(episode_dir / cur["dialogue_wav"]), "-af",
               f"aresample=48000,aformat=channel_layouts=mono,apad=whole_dur={s.duration_sec:.3f},"
               f"atrim=0:{s.duration_sec:.3f}",
               "-c:a", "pcm_s16le", str(part))
        else:
            ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", f"{s.duration_sec:.3f}", "-c:a", "pcm_s16le",
               str(part))
        parts.append(part)
    out = work / "dialogue.wav"
    listing = work / "dialogue.txt"
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "pcm_s16le", str(out))
    return out


def ambience_track(show_dir: Path, sl: Shotlist, bible, work: Path) -> Path:
    parts = []
    for s in sl.shots:
        loc = bible.locations[s.location_id]
        part = work / f"amb_{s.shot_id}.wav"
        custom = bible.location_dir(s.location_id) / loc.ambience
        if custom.is_file():
            ff("-stream_loop", "-1", "-i", str(custom), "-t", f"{s.duration_sec:.3f}", "-af",
               "aresample=48000,aformat=channel_layouts=stereo", "-c:a", "pcm_s16le", str(part))
        else:
            colour, _, chain = ROOM_TONES.get(loc.ambience, ROOM_TONES["night"])
            ff("-f", "lavfi", "-i", f"anoisesrc=color={colour}:sample_rate=48000:seed=7", "-t", f"{s.duration_sec:.3f}",
               "-af", f"{chain},aformat=channel_layouts=stereo", "-c:a", "pcm_s16le", str(part))
        parts.append(part)
    out = work / "ambience.wav"
    listing = work / "ambience.txt"
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "pcm_s16le", str(out))
    return out


def sfx_events(root: Path, show_dir: Path, sl: Shotlist) -> tuple[list[tuple[Path, float, float]], list[str]]:
    events, missing, t = [], [], 0.0
    for s in sl.shots:
        for cue in s.sfx_cues:
            path = next((p for p in (show_dir / cue.file, root / cue.file) if p.is_file()), None)
            if path is None:
                missing.append(f"{s.shot_id}: {cue.file}")
            else:
                events.append((path, t + cue.at_sec, cue.gain_db))
        t += s.duration_sec
    return events, missing


def mix_graph(n_sfx: int, has_music: bool, total: float, a: dict) -> str:
    """Inputs: 0 dialogue, 1 ambience, 2..2+n_sfx-1 sfx (already delayed), then music."""
    g = [f"[0:a]aformat=channel_layouts=stereo,loudnorm=I={a['dialogue_lufs']}:TP=-2:LRA=11,aresample=48000,"
         f"asplit=3[dlg][sc1][sc2]",
         f"[1:a]loudnorm=I={a['ambience_lufs']}:TP=-6:LRA=7,aresample=48000[amb0]",
         f"[amb0][sc1]sidechaincompress=threshold={a['duck_threshold']}:ratio=3:attack={a['duck_attack_ms']}:"
         f"release={a['duck_release_ms']}[amb]"]
    mix = ["[dlg]", "[amb]"]
    for k in range(n_sfx):
        g.append(f"[{2 + k}:a]aformat=channel_layouts=stereo,aresample=48000[fx{k}]")
        mix.append(f"[fx{k}]")
    if has_music:
        m = 2 + n_sfx
        g.append(f"[{m}:a]aformat=channel_layouts=stereo,atrim=0:{total:.3f},asetpts=N/SR/TB,"
                 f"loudnorm=I={a['music_lufs']}:TP=-4:LRA=9,aresample=48000,afade=t=in:st=0:d=1.5,"
                 f"afade=t=out:st={max(0.0, total - 2.5):.3f}:d=2.5[mus0]")
        g.append(f"[mus0][sc2]sidechaincompress=threshold={a['duck_threshold']}:ratio={a['duck_ratio']}:"
                 f"attack={a['duck_attack_ms']}:release={a['duck_release_ms']}[mus]")
        mix.append("[mus]")
    else:
        g.append("[sc2]anullsink")
    g.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first,"
             f"loudnorm=I={a['master_lufs']}:TP={a['true_peak_db']}:LRA=11,aresample=48000,"
             f"atrim=0:{total:.3f}[out]")
    return ";".join(g)


def mix_audio(root: Path, show_dir: Path, episode_dir: Path, sl: Shotlist, bible, cfg: dict, music: Path | None,
              work: Path) -> tuple[Path, list[str]]:
    total = sl.runtime
    dlg = dialogue_track(episode_dir, sl, work)
    amb = ambience_track(show_dir, sl, bible, work)
    events, missing = sfx_events(root, show_dir, sl)
    args = ["-i", str(dlg), "-i", str(amb)]
    sfx_files = []
    for k, (path, at, gain) in enumerate(events):
        placed = work / f"sfx_{k:02d}.wav"
        ff("-i", str(path), "-af", f"aresample=48000,aformat=channel_layouts=stereo,"
           f"loudnorm=I={cfg['audio']['sfx_lufs']}:TP=-3,volume={gain}dB,adelay={int(at * 1000)}:all=1,"
           f"apad=whole_dur={total:.3f}", "-t", f"{total:.3f}", "-c:a", "pcm_s16le", str(placed))
        sfx_files.append(placed)
        args += ["-i", str(placed)]
    if music is not None:
        args += ["-stream_loop", "-1", "-i", str(music)]
    pre = work / "mix_pre.wav"
    ff(*args, "-filter_complex", mix_graph(len(sfx_files), music is not None, total, cfg["audio"]),
       "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "48000", str(pre))
    out = work / "mix.wav"
    two_pass_loudnorm(pre, out, cfg["audio"]["master_lufs"], cfg["audio"]["true_peak_db"])
    return out, missing


def two_pass_loudnorm(src: Path, dest: Path, target: float, tp: float) -> dict:
    """Measure, then apply linear normalization: accurate on short clips where one pass undershoots."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-af", f"loudnorm=I={target}:TP={tp}:LRA=11:print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])
    ff("-i", str(src), "-af", f"loudnorm=I={target}:TP={tp}:LRA=11:measured_I={m['input_i']}:"
       f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
       f"offset={m['target_offset']}:linear=true,aresample=48000", "-c:a", "pcm_s16le", str(dest))
    return m


# ======================================================================== masters
def episode_master(root: Path, show_dir: Path, episode_dir: Path, sl: Shotlist, bible, cfg: dict,
                   music: Path | None, work: Path, dest: Path) -> dict:
    r = cfg["render"]
    w, h, fps, crf = r["width"], r["height"], r["fps"], r["crf"]
    work.mkdir(parents=True, exist_ok=True)
    font = load_font(round(h * 0.034))
    segs = []
    for i, s in enumerate(sl.shots):
        src = shot_video(episode_dir, s)
        window = caption_window(s, cfg)
        cap = draw_caption(work / f"cap_{s.shot_id}.png", s.dialogue_line, font, w, h,
                           CAPTION_Y[s.caption_zone]) if window else None
        segs.append(normalize_segment(src, work / f"seg_{i:02d}.mp4", s, w, h, fps, fades(sl, i), cap, window, crf))
    picture = concat(segs, work / "picture.mp4", work)
    mix, missing = mix_audio(root, show_dir, episode_dir, sl, bible, cfg, music, work)
    ff("-i", str(picture), "-i", str(mix), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
       "-b:a", "192k", "-ar", "48000", "-t", f"{sl.runtime:.3f}", "-movflags", "+faststart", str(dest))
    return {"master": str(dest), "segments": [str(p) for p in segs], "missing_sfx": missing,
            "music": str(music) if music else None}


# ======================================================================== 16:9 reframing
def reframe_plan(video: Path, shot: Shot, frames_dir: Path, wide_w: int = 1920, wide_h: int = 1080,
                 max_upscale: float = 1.5) -> dict:
    """Decide how a 9:16 shot sits in 16:9 without cutting faces.
    fill   = full-width band centred on the faces (band ~32% of the portrait height)
    framed = square crop around the faces over a blurred full-bleed copy (faces up to ~56% of height)
    full   = the whole portrait frame over a blurred copy (faces spread over more than that).
    Quality guard: a layout is used only if it upscales the source by at most `max_upscale`
    (a 720p portrait source cannot fill 1920 px wide sharply, so it is framed instead)."""
    info = next(s for s in vision.probe(video)["streams"] if s["codec_type"] == "video")
    sw, sh = int(info["width"]), int(info["height"])
    band = (wide_h / wide_w) * sw / sh
    square = sw / sh
    length = min(shot.duration_sec, vision.duration(video))
    frames = vision.extract_frames(video, frames_dir, [length * f for f in (0.15, 0.5, 0.85)])
    boxes = [b for bs in vision.faces(frames).values() for b in bs]
    if boxes:
        y0 = max(0.0, min(b[1] for b in boxes) - 0.04)
        y1 = min(1.0, max(b[1] + b[3] for b in boxes) + 0.06)
        centre, span, source = (y0 + y1) / 2, y1 - y0, "faces"
    else:
        centre = shot.wide_focus_y if shot.wide_focus_y is not None else FOCUS_BY_FRAMING.get(shot.framing, 0.42)
        span, source = 0.0, "shotlist" if shot.wide_focus_y is not None else "default"
    upscale = {"fill": wide_w / sw, "framed": wide_h / sw, "full": wide_h / sh}
    if span <= band and upscale["fill"] <= max_upscale:
        layout = "fill"
    elif span <= square and upscale["framed"] <= max_upscale:
        layout = "framed"
    else:
        layout = "full"
    size = band if layout == "fill" else square if layout == "framed" else 1.0
    top = min(max(0.0, centre - size / 2), max(0.0, 1.0 - size))
    return {"shot_id": shot.shot_id, "layout": layout, "top": round(top, 4), "size": round(size, 4),
            "focus_source": source, "faces": len(boxes), "upscale": round(upscale[layout], 2)}


def wide_filter(plan: dict, w: int, h: int) -> str:
    top = plan["top"]
    if plan["layout"] == "fill":
        return (f"scale={w}:-2:flags=lanczos,crop={w}:{h}:0:'min(ih-{h},max(0,ih*{top}))',setsar=1")
    bg = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=28:2,"
          f"eq=brightness=-0.12:saturation=0.8")
    if plan["layout"] == "framed":
        fg = f"crop=iw:iw:0:'min(ih-iw,max(0,ih*{top}))',scale={h}:{h}:flags=lanczos"
    else:
        fg = f"scale=-2:{h}:flags=lanczos"
    return f"split=2[bgs][fgs];[bgs]{bg}[bg];[fgs]{fg}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"


def episode_wide(episode_dir: Path, sl: Shotlist, cfg: dict, master_9x16: Path, work: Path, dest: Path) -> dict:
    """16:9 cut of an approved episode from the SAME paid shots and the master's exact audio mix."""
    r = cfg["render"]
    w, h, fps, crf = r["wide_width"], r["wide_height"], r["fps"], r["crf"]
    work.mkdir(parents=True, exist_ok=True)
    font = load_font(round(h * 0.055))
    plans, segs = [], []
    for i, s in enumerate(sl.shots):
        src = shot_video(episode_dir, s)
        plan = reframe_plan(src, s, work / "frames", w, h, r.get("max_upscale_16x9", 1.5))
        plans.append(plan)
        window = caption_window(s, cfg)
        cap = draw_caption(work / f"wcap_{s.shot_id}.png", s.dialogue_line, font, w, h, 0.87) if window else None
        vf = (f"fps={fps},{wide_filter(plan, w, h)},tpad=stop_mode=clone:stop_duration=1,"
              f"trim=duration={s.duration_sec:.3f},setpts=PTS-STARTPTS{fades(sl, i)},format=yuv420p")
        seg = work / f"wseg_{i:02d}.mp4"
        if cap is not None:
            a, b = window
            graph = (f"[0:v]{vf}[b];[1:v]format=rgba[c];"
                     f"[b][c]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})',format=yuv420p[v]")
            ff("-i", str(src), "-loop", "1", "-i", str(cap), "-filter_complex", graph, "-map", "[v]", "-an",
               "-t", f"{s.duration_sec:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-r", str(fps),
               str(seg))
        else:
            ff("-i", str(src), "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-an", "-t", f"{s.duration_sec:.3f}",
               "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-r", str(fps), str(seg))
        segs.append(seg)
    picture = concat(segs, work / "wide_picture.mp4", work)
    ff("-i", str(picture), "-i", str(master_9x16), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest",
       "-movflags", "+faststart", str(dest))
    (episode_dir / "reports").mkdir(exist_ok=True)
    (episode_dir / "reports" / "reframe_16x9.json").write_text(json.dumps(plans, indent=2) + "\n", encoding="utf-8")
    return {"wide": str(dest), "layouts": {p["layout"]: sum(1 for q in plans if q["layout"] == p["layout"])
                                           for p in plans}}


def bumper(dest: Path, title: str, subtitle: str, seconds: float, cfg: dict) -> Path:
    r = cfg["render"]
    w, h, fps = r["wide_width"], r["wide_height"], r["fps"]
    card = dest.with_suffix(".png")
    img = Image.new("RGB", (w, h), (8, 8, 10))
    d = ImageDraw.Draw(img)
    big, small = load_font(round(h * 0.075)), load_font(round(h * 0.04))
    d.text(((w - d.textlength(title, font=big)) / 2, h * 0.40), title, font=big, fill=(240, 236, 228))
    d.text(((w - d.textlength(subtitle, font=small)) / 2, h * 0.53), subtitle, font=small, fill=(170, 165, 158))
    img.save(card)
    ff("-loop", "1", "-i", str(card), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.3f}",
       "-vf", f"fps={fps},format=yuv420p,fade=t=in:st=0:d=0.5,fade=t=out:st={seconds - 0.5:.3f}:d=0.5",
       "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(fps), "-c:a", "aac", "-b:a", "192k",
       "-ar", "48000", str(dest))
    return dest


def compilation(parts: list[tuple[str, Path]], arc_title: str, cfg: dict, work: Path, dest: Path) -> dict:
    """Arc cut: opening card, then (episode card + episode 16:9 cut) for each episode, joined without re-encoding
    the episodes (all parts share codec, size, fps and audio format)."""
    work.mkdir(parents=True, exist_ok=True)
    secs = cfg["compilation"]["bumper_seconds"]
    seq = [bumper(work / "open.mp4", arc_title, "TaleNovi Studio original series", secs + 0.5, cfg)]
    for i, (label, path) in enumerate(parts):
        seq.append(bumper(work / f"card_{i:02d}.mp4", label.split(" | ")[0], label.split(" | ")[-1], secs, cfg))
        seq.append(path)
    partial = dest.with_name("." + dest.stem + ".partial.mp4")
    listing = work / "arc.txt"
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in seq), encoding="utf-8")
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
       "-ar", "48000", "-movflags", "+faststart", str(partial))
    partial.replace(dest)
    return {"compilation": str(dest), "seconds": round(vision.duration(dest), 2), "parts": len(parts)}
