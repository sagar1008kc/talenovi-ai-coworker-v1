"""episodes/epXX/shotlist.json: validation against the locked bible and the shot state machine.

    pending --(voice+motion+lip sync)--> rendered --(QA pass)--> approved
                                           |                       |
                                           +--(QA fail)--> qa_failed <--(reject-shot)
    qa_failed --(retry, attempts <= max_retries_per_shot)--> pending

Only the failed shot moves; every other shot keeps its status and its paid assets.
"""
from __future__ import annotations

import json
from pathlib import Path

from studio.director.bible import Bible
from studio.director.schema import Shot, Shotlist
from studio.models import utc_now

SHOTLIST = "shotlist.json"
VOICE_CHECKS = {"stt_dialogue", "dialogue_fits"}
PICTURE_CHECKS = {"video_valid", "duration", "motion", "speaker_face", "identity", "lip_sync_alignment"}
ALLOWED = {"pending": {"rendered", "qa_failed"}, "rendered": {"approved", "qa_failed"},
           "qa_failed": {"pending"}, "approved": {"qa_failed"}}


class ShotStateError(ValueError):
    pass


class RetryLimitReached(RuntimeError):
    pass


def load_shotlist(episode_dir: Path) -> Shotlist:
    return Shotlist.model_validate_json((episode_dir / SHOTLIST).read_text(encoding="utf-8"))


def save_shotlist(episode_dir: Path, sl: Shotlist) -> None:
    path = episode_dir / SHOTLIST
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(sl.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def transition(shot: Shot, new: str, reason: str = "") -> None:
    if new not in ALLOWED[shot.status]:
        raise ShotStateError(f"{shot.shot_id}: cannot go from {shot.status} to {new}")
    shot.history.append({"utc": utc_now(), "from": shot.status, "to": new, "reason": reason})
    shot.status = new  # type: ignore[assignment]


def redo_parts(shot: Shot) -> list[str]:
    """Which parts of a failed take must be regenerated; everything else is reused (never paid twice).
    An editorial rejection names its parts explicitly (reject-shot --redo); unknown failures redo all."""
    failed = {c["name"] for c in shot.qa.get("checks", []) if not c["passed"] and c.get("severity") == "fail"}
    explicit = shot.qa.get("redo")
    if explicit:
        return sorted(set(explicit))
    parts = set()
    if failed & VOICE_CHECKS:
        parts |= {"voice", "lipsync"}
    if failed & PICTURE_CHECKS or failed - VOICE_CHECKS - PICTURE_CHECKS:
        parts |= {"motion", "lipsync"}
    return sorted(parts or {"voice", "motion", "lipsync"})


def retry(shot: Shot, max_retries: int, reason: str) -> None:
    """qa_failed -> pending for ONE shot. Refuses past the per-shot retry cap (stop and alert)."""
    if shot.status != "qa_failed":
        raise ShotStateError(f"{shot.shot_id}: only qa_failed shots are retried (is {shot.status})")
    if shot.attempts > max_retries:
        raise RetryLimitReached(f"{shot.shot_id}: {shot.attempts} attempts; retry cap {max_retries} reached. "
                                "Revise this shot in shotlist.json (staging, action, length) or ask the owner.")
    archived = shot.assets.pop("current", None)
    if archived:
        archived["failed_checks"] = [c["name"] for c in shot.qa.get("checks", []) if not c["passed"]]
        archived["redo"] = redo_parts(shot)
        shot.assets.setdefault("rejected", []).append(archived)
    shot.qa = {}
    transition(shot, "pending", reason)


def check_shotlist(sl: Shotlist, bible: Bible, fmt: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings). Errors block any paid call."""
    errors, warnings = [], []
    n = len(sl.shots)
    if sl.pilot:
        warnings.append("PILOT: format limits skipped; this episode can never be approved or uploaded")
    elif not fmt["min_shots"] <= n <= fmt["max_shots"]:
        errors.append(f"{n} shots; an episode needs {fmt['min_shots']}-{fmt['max_shots']}")
    total = sl.runtime
    if sl.pilot:
        pass
    elif not fmt["min_seconds"] <= total <= fmt["max_seconds"]:
        errors.append(f"runtime {total:.1f} s outside {fmt['min_seconds']}-{fmt['max_seconds']} s")
    elif abs(total - fmt["target_seconds"]) > 3:
        warnings.append(f"runtime {total:.1f} s; target is {fmt['target_seconds']} s")
    lead, tail = fmt["dialogue_lead_seconds"], fmt["dialogue_tail_seconds"]
    for s in sl.shots:
        if not fmt["shot_min_seconds"] <= s.duration_sec <= fmt["shot_max_seconds"]:
            errors.append(f"{s.shot_id}: {s.duration_sec} s outside {fmt['shot_min_seconds']}-"
                          f"{fmt['shot_max_seconds']} s")
        if s.motion_model:
            warnings.append(f"{s.shot_id}: motion model override {s.motion_model}")
        if s.location_id not in bible.locations:
            errors.append(f"{s.shot_id}: unknown location_id {s.location_id!r} (add it to bible/locations)")
        for cid in s.character_ids:
            if cid not in bible.characters:
                errors.append(f"{s.shot_id}: unknown character_id {cid!r} (add it to bible/characters)")
        if not s.action.strip():
            errors.append(f"{s.shot_id}: every shot needs one visible action")
        if s.dialogue_line.strip():
            voice = bible.voices.get(s.speaker_id or "")
            if voice is None:
                errors.append(f"{s.shot_id}: speaker {s.speaker_id!r} has no locked voice")
            elif s.voice_id != voice.voice_env_key:
                errors.append(f"{s.shot_id}: voice_id {s.voice_id!r} is not {s.speaker_id}'s locked voice "
                              f"{voice.voice_env_key!r}")
            words = len(s.dialogue_line.split())
            est = words / 2.6
            if est + lead + tail > s.duration_sec:
                warnings.append(f"{s.shot_id}: ~{est:.1f} s of speech may not fit {s.duration_sec} s "
                                "(measured exactly after TTS)")
        if s.lip_sync_required and s.framing in ("wide", "insert"):
            warnings.append(f"{s.shot_id}: lip sync on a {s.framing} shot; the mouth may be too small to read")
        for cue in s.sfx_cues:
            if cue.at_sec >= s.duration_sec:
                errors.append(f"{s.shot_id}: sfx cue at {cue.at_sec} s is past the shot end")
    if not sl.pilot and (not sl.canon_updates or not sl.next_episode_starts or not sl.character_knowledge):
        warnings.append("canon_updates, character_knowledge and next_episode_starts are required before approve")
    if not any(s.dialogue_line for s in sl.shots):
        warnings.append("no dialogue in this episode")
    return errors, warnings


def episode_state(sl: Shotlist) -> dict:
    counts: dict[str, int] = {}
    for s in sl.shots:
        counts[s.status] = counts.get(s.status, 0) + 1
    return {"shots": len(sl.shots), "by_status": counts, "runtime_sec": sl.runtime,
            "all_approved": all(s.status == "approved" for s in sl.shots),
            "cost_usd": round(sum(s.cost_usd for s in sl.shots), 4)}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
