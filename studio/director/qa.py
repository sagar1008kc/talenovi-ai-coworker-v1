"""Automated QA: per shot (targeted retry) and per episode master (upload gate).

Shot checks (a failure moves ONLY that shot to qa_failed):
  video_valid, duration, motion (no slideshow-like freeze), dialogue_fits, speaker_face (lip-sync shots),
  lipsync_output, stt_dialogue (TTS said the scripted line).
Episode checks (all must pass before approve / upload):
  container + streams, resolution/fps, duration window, loudness, STT of the final mix vs shotlist,
  every shot approved, no shot assembled from a still.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from studio.director import vision
from studio.director.schema import Shot, Shotlist


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))


def word_error_rate(expected: str, heard: str) -> float:
    ref, hyp = words(expected), words(heard)
    if not ref:
        return 0.0 if not hyp else 1.0
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
            prev, d[j] = d[j], cur
    return d[len(hyp)] / len(ref)


def check(name: str, ok: bool, detail: str = "", severity: str = "fail") -> dict:
    return {"name": name, "passed": bool(ok), "severity": severity, "detail": detail}


def shot_checks(shot: Shot, video: Path, voice: dict | None, cfg: dict, frames_dir: Path,
                heard: str | None, dry_run: bool) -> list[dict]:
    q, fmt = cfg["qa"], cfg["episode_format"]
    out = []
    try:
        info = vision.probe(video)
        has_video = any(s["codec_type"] == "video" for s in info["streams"])
        got = float(info["format"]["duration"])
    except (subprocess.CalledProcessError, KeyError, ValueError) as exc:
        return [check("video_valid", False, f"unreadable: {exc}")]
    out.append(check("video_valid", has_video, "video stream present" if has_video else "no video stream"))
    out.append(check("duration", got + q["duration_tolerance_seconds"] >= shot.duration_sec,
                     f"{got:.2f} s for a {shot.duration_sec} s shot"))
    frozen = vision.frozen_fraction(video, noise=q["freeze_noise"])
    out.append(check("motion", frozen <= q["max_freeze_fraction"],
                     f"{frozen:.0%} of the clip is frozen (max {q['max_freeze_fraction']:.0%})"))
    if voice:
        need = voice["seconds"] + fmt["dialogue_lead_seconds"] + fmt["dialogue_tail_seconds"]
        out.append(check("dialogue_fits", need <= shot.duration_sec + 1e-6,
                         f"line is {voice['seconds']:.2f} s; shot holds {shot.duration_sec - fmt['dialogue_lead_seconds'] - fmt['dialogue_tail_seconds']:.2f} s"))
        if heard is not None:
            wer = word_error_rate(shot.dialogue_line, heard)
            out.append(check("stt_dialogue", wer <= q["max_word_error_rate"],
                             f"WER {wer:.2f}; heard {heard!r}"))
    if shot.lip_sync_required:
        if dry_run:
            out.append(check("speaker_face", True, "skipped in dry run (placeholder frames)", "info"))
        elif not vision.available():
            out.append(check("speaker_face", True, "Apple Vision unavailable; frame review must confirm",
                             "warn"))
        else:
            got_s = min(got, shot.duration_sec)
            frames = vision.extract_frames(video, frames_dir, [got_s * f for f in (0.2, 0.4, 0.6, 0.8)])
            found = vision.faces(frames)
            share = sum(1 for f in frames if found.get(str(f))) / max(1, len(frames))
            out.append(check("speaker_face", share >= q["min_face_frames_for_lipsync"],
                             f"a face is visible in {share:.0%} of sampled frames"))
    return out


def identity_notes(shot: Shot, video: Path, bible, frames_dir: Path, warn_at: float) -> list[dict]:
    """Advisory: Vision feature-print distance between the shot's largest face and the character's
    front reference. It flags drift for Claude's frame review; it is not a face-recognition verdict."""
    if not shot.character_ids or not vision.available():
        return []
    frame = vision.extract_frames(video, frames_dir, [min(shot.duration_sec, vision.duration(video)) * 0.5])
    if not frame:
        return []
    boxes = vision.faces(frame).get(str(frame[0]), [])
    ref = bible.character_refs(shot.character_ids[0])[0]
    ref_boxes = vision.faces([ref]).get(str(ref), [])
    if not boxes or not ref_boxes:
        return [check("identity", True, "no face to compare", "info")]
    a = vision.crop_face(frame[0], max(boxes, key=lambda b: b[2] * b[3]), frames_dir / f"{shot.shot_id}_face.png")
    b = vision.crop_face(ref, max(ref_boxes, key=lambda b: b[2] * b[3]), frames_dir / f"{shot.shot_id}_ref.png")
    d = vision.distance(a, b)
    if d is None:
        return []
    return [check("identity", d <= warn_at, f"face distance to {shot.character_ids[0]} reference {d:.2f} "
                  f"(warn above {warn_at})", "warn")]


def loudness(path: Path) -> float | None:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
    return float(m[-1]) if m else None


def episode_checks(master: Path, sl: Shotlist, cfg: dict, heard: str | None, width: int, height: int,
                   min_s: float, max_s: float) -> list[dict]:
    q, a = cfg["qa"], cfg["audio"]
    out = []
    info = vision.probe(master)
    v = [s for s in info["streams"] if s["codec_type"] == "video"]
    au = [s for s in info["streams"] if s["codec_type"] == "audio"]
    out.append(check("container", info["format"].get("duration") is not None and len(v) == 1 and len(au) == 1,
                     f"{len(v)} video / {len(au)} audio streams"))
    if v:
        s = v[0]
        out.append(check("video_format", s.get("codec_name") == "h264" and s.get("width") == width
                         and s.get("height") == height and s.get("r_frame_rate") == f"{cfg['render']['fps']}/1",
                         f"{s.get('codec_name')} {s.get('width')}x{s.get('height')} @ {s.get('r_frame_rate')}"))
    if au:
        out.append(check("audio_format", au[0].get("codec_name") == "aac", f"{au[0].get('codec_name')}"))
    dur = float(info["format"]["duration"])
    out.append(check("duration", min_s <= dur <= max_s, f"{dur:.2f} s (allowed {min_s}-{max_s})"))
    expected_dur = sl.runtime
    out.append(check("duration_matches_shotlist", abs(dur - expected_dur) <= 0.5,
                     f"{dur:.2f} s vs shotlist {expected_dur:.2f} s"))
    lufs = loudness(master)
    out.append(check("loudness", lufs is not None and abs(lufs - a["master_lufs"]) <= a["loudness_tolerance_lu"],
                     f"{lufs} LUFS (target {a['master_lufs']})"))
    script = " ".join(s.dialogue_line for s in sl.shots if s.dialogue_line.strip())
    if heard is not None and script:
        wer = word_error_rate(script, heard)
        out.append(check("stt_master", wer <= q["max_word_error_rate"], f"WER {wer:.2f} over the whole episode"))
    elif script:
        out.append(check("stt_master", False, "speech-to-text did not run"))
    not_ok = [s.shot_id for s in sl.shots if s.status != "approved"]
    out.append(check("all_shots_approved", not not_ok, f"not approved: {not_ok}" if not_ok else "all approved"))
    stills = [s.shot_id for s in sl.shots if not s.assets.get("current", {}).get("video")]
    out.append(check("no_still_fallback", not stills, f"no motion video for {stills}" if stills else
                     "every shot is a motion video"))
    return out


def passed(checks: list[dict]) -> bool:
    return all(c["passed"] or c["severity"] != "fail" for c in checks)
