"""Native lip sync, step 2: retime our locked-voice line onto the motion model's own performance.

Seedance (native mode) re-performs the scripted line with its own timing and voice, and the mouths
follow THAT performance. We keep the ElevenLabs voice (locked identity) and move it onto the
performance: speech-to-text gives the performance's word timings, the TTS alignment gives ours,
and each phrase of our line is time-stretched (atempo) into the matching phrase window.
If the performance does not say the scripted words, the shot fails (`lip_sync_alignment`) and only
its motion is regenerated.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from studio.director.qa import words as norm_words

MIN_TEMPO, MAX_TEMPO = 0.67, 1.5     # beyond this our voice would sound unnaturally stretched
PHRASE_GAP = 0.22                    # a pause longer than this in the performance splits phrases


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, capture_output=True)


def our_words(alignment: dict) -> list[tuple[str, float, float]]:
    """ElevenLabs character alignment -> [(word, start, end)] in the TTS file's own time."""
    chars = alignment["characters"]
    starts, ends = alignment["character_start_times_seconds"], alignment["character_end_times_seconds"]
    out, cur, s0, e0 = [], "", None, None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                out.append((cur, s0, e0))
            cur, s0 = "", None
            continue
        if s0 is None:
            s0 = s
        cur += ch
        e0 = e
    if cur:
        out.append((cur, s0, e0))
    return [w for w in out if norm_words(w[0])]


def phrases(words: list[tuple[str, float, float]], gap: float = PHRASE_GAP) -> list[tuple[int, int]]:
    groups, start = [], 0
    for i in range(1, len(words)):
        if words[i][1] - words[i - 1][2] > gap:
            groups.append((start, i - 1))
            start = i
    groups.append((start, len(words) - 1))
    return groups


def align(tts_audio: Path, alignment: dict, performance: list[tuple[str, float, float]], shot_seconds: float,
          dest: Path) -> dict:
    """Write `dest` (mono 48 kHz WAV, shot length) with our line placed on the performance timing."""
    ours = our_words(alignment)
    a = [t for w in ours for t in norm_words(w[0])]
    b = [t for w in performance for t in norm_words(w[0])]
    if a != b or len(ours) != len(performance):
        return {"ok": False, "reason": f"performance words {b} differ from the line {a}"}
    if performance[-1][2] > shot_seconds + 0.05:
        return {"ok": False, "reason": f"performance ends at {performance[-1][2]:.2f} s, after the shot"}
    report, parts = [], []
    for k, (i, j) in enumerate(phrases(performance)):
        src0, src1 = max(0.0, ours[i][1] - 0.02), ours[j][2] + 0.04
        dst0, dst1 = performance[i][1], performance[j][2]
        tempo = (src1 - src0) / max(0.05, dst1 - dst0 + 0.06)
        report.append({"phrase": " ".join(w[0] for w in ours[i:j + 1]), "ours": [round(src0, 3), round(src1, 3)],
                       "performance": [round(dst0, 3), round(dst1, 3)], "tempo": round(tempo, 3)})
        part = dest.with_name(f"{dest.stem}_p{k}.wav")
        t = min(2.0, max(0.5, tempo))
        ff("-i", str(tts_audio), "-af", f"atrim={src0:.3f}:{src1:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
           f"aformat=channel_layouts=mono,atempo={t:.4f},adelay={int(dst0 * 1000)},"
           f"apad=whole_dur={shot_seconds:.3f}", "-t", f"{shot_seconds:.3f}",
           "-c:a", "pcm_s16le", str(part))
        parts.append(part)
    args = [x for p in parts for x in ("-i", str(p))]
    ff(*args, "-filter_complex", f"amix=inputs={len(parts)}:normalize=0:duration=longest,apad=whole_dur={shot_seconds:.3f}",
       "-t", f"{shot_seconds:.3f}", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1", str(dest))
    for p in parts:
        p.unlink(missing_ok=True)
    worst = max(report, key=lambda r: abs(r["tempo"] - 1))["tempo"]
    ok = all(MIN_TEMPO <= r["tempo"] <= MAX_TEMPO for r in report)
    return {"ok": ok, "phrases": report, "worst_tempo": worst,
            "speech_start": performance[0][1], "speech_end": performance[-1][2],
            "reason": "" if ok else f"phrase tempo {worst} outside {MIN_TEMPO}-{MAX_TEMPO}"}


def load_alignment(tts_audio: Path) -> dict:
    return json.loads(tts_audio.with_suffix(".alignment.json").read_text(encoding="utf-8"))
