"""Automated quality gates. Passing them is necessary, NOT sufficient: editorial quality, rights
and platform eligibility still require a human. Each check returns a structured result.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from PIL import Image
from pydantic import BaseModel

from studio.captions import parse_srt
from studio.models import Asset, ResearchBrief, RenderProfile, Storyboard


class Check(BaseModel):
    name: str
    status: str  # pass | warn | fail
    detail: str = ""


class QCReport(BaseModel):
    profile: str
    video: str
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def summary(self) -> dict:
        return {"profile": self.profile, "video": self.video, "passed": self.passed,
                "failures": [c.model_dump() for c in self.checks if c.status == "fail"],
                "warnings": [c.model_dump() for c in self.checks if c.status == "warn"],
                "checks": [c.model_dump() for c in self.checks],
                "human_review_required": True}


def _ffmpeg_filter_log(video: Path, args: list[str]) -> str:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), *args, "-f", "null", "-"],
                          capture_output=True, text=True)
    return proc.stderr


# --------------------------------------------------------------------------- technical checks
def technical_checks(video: Path, profile: RenderProfile) -> list[Check]:
    checks: list[Check] = []
    try:
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                          "-show_format", str(video)], check=True, capture_output=True,
                                         text=True).stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        return [Check(name="container", status="fail", detail=f"ffprobe could not read file: {exc}")]
    fmt = info.get("format", {})
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    checks.append(Check(name="container", status="pass" if "mp4" in fmt.get("format_name", "") else "fail",
                        detail=fmt.get("format_name", "")))
    if v is None:
        return checks + [Check(name="video_stream", status="fail", detail="no video stream")]
    checks.append(Check(name="video_codec", status="pass" if v.get("codec_name") == "h264" and
                        v.get("pix_fmt") == "yuv420p" else "fail",
                        detail=f"{v.get('codec_name')} {v.get('pix_fmt')}"))
    dims = (int(v["width"]), int(v["height"]))
    checks.append(Check(name="resolution", status="pass" if dims == (profile.width, profile.height) else "fail",
                        detail=f"{dims[0]}x{dims[1]} expected {profile.width}x{profile.height}"))
    checks.append(Check(name="audio_stream", status="pass" if a and a.get("codec_name") == "aac" else "fail",
                        detail=(a or {}).get("codec_name", "missing")))
    duration = float(fmt.get("duration", 0))
    in_range = profile.min_seconds <= duration <= profile.max_seconds
    checks.append(Check(name="duration", status="pass" if in_range else "fail",
                        detail=f"{duration:.1f}s (allowed {profile.min_seconds:.0f}-{profile.max_seconds:.0f}s)"))
    if a:
        av_gap = abs(float(v.get("duration", duration)) - float(a.get("duration", duration)))
        checks.append(Check(name="av_sync_length", status="pass" if av_gap <= 0.25 else "fail",
                            detail=f"video/audio stream length differ by {av_gap:.2f}s"))

    decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"],
                            capture_output=True, text=True).stderr.strip()
    checks.append(Check(name="decode_errors", status="pass" if not decode else "fail", detail=decode[:300]))

    black = _ffmpeg_filter_log(video, ["-vf", "blackdetect=d=1.0:pix_th=0.10", "-an"])
    black_spans = re.findall(r"black_duration:([\d.]+)", black)
    checks.append(Check(name="black_frames", status="fail" if black_spans else "pass",
                        detail=f"{len(black_spans)} black span(s) >= 1s"))

    frozen = _ffmpeg_filter_log(video, ["-vf", "freezedetect=n=0.001:d=10", "-an"])
    freezes = re.findall(r"freeze_duration: ([\d.]+)", frozen)
    checks.append(Check(name="frozen_frames", status="fail" if freezes else "pass",
                        detail=f"{len(freezes)} frozen span(s) >= 10s"))

    silence = _ffmpeg_filter_log(video, ["-af", "silencedetect=n=-45dB:d=2.5", "-vn"])
    silences = re.findall(r"silence_duration: ([\d.]+)", silence)
    checks.append(Check(name="unexpected_silence", status="fail" if silences else "pass",
                        detail=f"{len(silences)} silent span(s) >= 2.5s"))

    loud = _ffmpeg_filter_log(video, ["-af", "ebur128=peak=true", "-vn"])
    integ = re.findall(r"I:\s+(-?[\d.]+) LUFS", loud)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", loud)
    if integ:
        lufs = float(integ[-1])
        tp = float(peak[-1]) if peak else -99.0
        ok = -18.0 <= lufs <= -11.0 and tp <= -0.5
        checks.append(Check(name="loudness", status="pass" if ok else "fail",
                            detail=f"{lufs:.1f} LUFS integrated, true peak {tp:.1f} dBFS"))
    else:
        checks.append(Check(name="loudness", status="warn", detail="could not measure loudness"))
    return checks


# --------------------------------------------------------------------------- content checks
def caption_checks(srt: Path | None, narration_words: int, duration: float,
                   final_hold: float = 0.0) -> list[Check]:
    """final_hold: deliberate reaction silence after the last line (storyboard scene_holds), allowed in the tail."""
    if srt is None or not srt.is_file():
        return [Check(name="captions", status="fail", detail="caption file missing")]
    cues = parse_srt(srt)
    if not cues:
        return [Check(name="captions", status="fail", detail="no caption cues")]
    monotonic = all(b.start >= a.start and a.end <= b.start + 0.05 for a, b in zip(cues, cues[1:]))
    words = sum(len(c.text.split()) for c in cues)
    coverage = words / max(1, narration_words)
    tail_gap = duration - cues[-1].end
    status = "pass" if monotonic and 0.95 <= coverage <= 1.05 and -0.1 <= tail_gap <= 3.0 + max(0.0, final_hold) else "fail"
    return [Check(name="captions", status=status,
                  detail=f"{len(cues)} cues, word coverage {coverage:.0%}, monotonic={monotonic}, "
                         f"last cue ends {tail_gap:.1f}s before video end")]


def narrative_checks(board: Storyboard, expected_narration: str, sections: int) -> list[Check]:
    norm = lambda t: re.sub(r"\s+", " ", t).strip()  # noqa: E731
    same = norm(board.narration_text()) == norm(expected_narration)
    ordered = all((a.section_index, a.start_seconds or 0) <= (b.section_index, b.start_seconds or 0)
                  for a, b in zip(board.shots, board.shots[1:]))
    covered = {s.section_index for s in board.shots} == set(range(sections))
    return [Check(name="narrative_coverage", status="pass" if same and covered else "fail",
                  detail=f"storyboard narration matches script={same}, all sections present={covered}"),
            Check(name="scene_order", status="pass" if ordered else "fail",
                  detail="shots ordered by section and timeline" if ordered else "shot order broken")]


def _ahash(path: Path) -> int:
    with Image.open(path) as img:
        small = img.convert("L").resize((8, 8), Image.BILINEAR)
        pixels = list(small.tobytes())
    avg = sum(pixels) / 64
    return sum(1 << i for i, p in enumerate(pixels) if p > avg)


def repeated_visual_checks(images: list[Path]) -> list[Check]:
    hashes = [_ahash(p) for p in images if p.is_file()]
    dupes = 0
    for i, h in enumerate(hashes):
        if any(bin(h ^ other).count("1") <= 3 for other in hashes[:max(0, i - 1)]):
            dupes += 1
    ratio = dupes / max(1, len(hashes))
    status = "fail" if ratio > 0.15 else "warn" if dupes else "pass"
    return [Check(name="repeated_visuals", status=status,
                  detail=f"{dupes} of {len(hashes)} shots near-duplicate an earlier non-adjacent shot")]


def factual_checks(brief: ResearchBrief, seen_urls: set[str], used_claim_ids: set[str],
                   ai_verdict: str | None) -> list[Check]:
    by_id = {c.claim_id: c for c in brief.claims}
    unknown = sorted(used_claim_ids - set(by_id))
    unsourced = sorted(cid for cid in used_claim_ids & set(by_id)
                       if not any(u in seen_urls for u in by_id[cid].source_urls))
    low = sorted(cid for cid in used_claim_ids & set(by_id) if by_id[cid].confidence == "low")
    checks = [
        Check(name="claims_exist", status="fail" if unknown else "pass",
              detail=f"script references unknown claim ids: {unknown}" if unknown else "all claim ids known"),
        Check(name="claims_sourced", status="fail" if unsourced else "pass",
              detail=(f"claims without a source seen in web search: {unsourced}" if unsourced
                      else "every used claim cites a URL returned by web search")),
        Check(name="low_confidence_claims", status="warn" if low else "pass", detail=", ".join(low)),
    ]
    if ai_verdict is not None:
        checks.append(Check(name="ai_fact_review", status={"pass": "pass", "revise": "warn"}.get(ai_verdict, "fail"),
                            detail=f"independent AI fact review verdict: {ai_verdict}"))
    return checks


def provenance_checks(assets: list[Asset], voice_license_confirmed: bool, music_required: bool) -> list[Check]:
    missing = [a.asset_id for a in assets if not (a.provider and a.model and a.license)]
    unconfirmed = sorted({a.provider for a in assets if a.commercial_use_confirmed is None})
    has_music = any(a.kind == "music" for a in assets)
    checks = [
        Check(name="asset_provenance", status="fail" if missing else "pass",
              detail=f"assets without provider/model/license: {missing}" if missing else
              f"{len(assets)} assets have provenance records"),
        Check(name="commercial_license", status="warn" if unconfirmed else "pass",
              detail=("human must confirm commercial terms for: " + ", ".join(unconfirmed)) if unconfirmed
              else "all assets marked commercially usable"),
        Check(name="voice_license", status="pass" if voice_license_confirmed else "warn",
              detail="voice.commercial_license_confirmed in config" if voice_license_confirmed
              else "confirm your ElevenLabs plan/voice permits commercial use"),
    ]
    if music_required and not has_music:
        checks.append(Check(name="music", status="warn", detail="no licensed music track available"))
    return checks
