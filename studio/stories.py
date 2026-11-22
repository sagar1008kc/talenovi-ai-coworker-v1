"""One folder per story (v6): story commands, central episode paths, duration limits, storyboard checks,
the per-run scratch manifest, the compact release record and safe post-release cleanup.

Layout for stories started with `story-start`:

    series/<slug>/series_bible.json, cast_lock.json, continuity.json, references/manifest.json
    series/<slug>/episodes/NN/story.json, storyboard.json, master_9x16.mp4, release.json (+ reports/, assets)

Older series (no `episodes/` folder) keep their `outputs/<slug>_epNN_<title>/` folders untouched.
No function here calls a paid provider or the network.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from studio.series import get_series, read_json, series_path, write_json

APPROVED_STAGE = "APPROVED_FOR_MANUAL_UPLOAD"
MANIFEST_NAME = "scratch_manifest.json"
RELEASE_NAME = "release.json"
# Measured on an approved episode (233 words of ElevenLabs speech in ~72 s); used for pre-production estimates.
WORDS_PER_SECOND = 3.1
DEFAULT_LIMITS = {"min_seconds": 60.0, "max_seconds": 90.0, "target_min_seconds": 75.0, "target_max_seconds": 85.0}
SHOT_SIZES = {"wide", "medium", "close", "close-up", "insert", "over_shoulder", "two_shot", "extreme_close"}
AUDIO_INTENTS = {"voiceover", "off_camera", "on_camera", "reaction", "silence", "ambient"}
CAPTION_ZONES = {"mid": 0.66, "high": 0.30}
# Never removed by cleanup, even if a manifest lists them.
PROTECTED_FILES = {"master_9x16.mp4", "story.json", "storyboard.json", "status.json", RELEASE_NAME, MANIFEST_NAME,
                   "captions.srt", "thumbnail.png", "story_preflight.json", "qc.json"}
PROTECTED_DIRS = ("reports", "visuals/images", "visuals/video_clips", "audio/audio_segments", "audio/music")
# Accepted paid sources kept for authorized re-edits (deduplicated, recorded in release.json).
RETAINED_DIRS = ("visuals/images", "visuals/video_clips", "audio/audio_segments", "audio/music")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ============================================================================ paths
def story_slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48].strip("-") or "story"


def uses_story_folders(root: Path, slug: str) -> bool:
    return (series_path(root, slug) / "episodes").is_dir()


def episode_path(root: Path, slug: str, number: int) -> Path:
    """The single physical home of an episode in the story-folder layout."""
    if not 1 <= number <= 99:
        raise ValueError("Episode numbers run 01-99")
    return series_path(root, slug) / "episodes" / f"{number:02}"


def is_story_episode(folder: Path) -> bool:
    folder = Path(folder)
    return (re.fullmatch(r"\d{2}", folder.name) is not None and folder.parent.name == "episodes"
            and folder.parent.parent.parent.name == "series")


def dry_run_target(folder: Path) -> Path:
    """Rehearsal folder. Story-folder episodes keep it inside the run directory (scratch, cleaned later)."""
    folder = Path(folder)
    return folder / "work" / "dryrun" if is_story_episode(folder) else folder.with_name(folder.name + "__dryrun")


def episode_dirs(root: Path, slug: str) -> dict[int, Path]:
    """Existing episode folders by number (story layout, or legacy outputs/<slug>_epNN_* folders)."""
    found: dict[int, Path] = {}
    if uses_story_folders(root, slug):
        for child in sorted((series_path(root, slug) / "episodes").iterdir()):
            if child.is_dir() and not child.is_symlink() and re.fullmatch(r"\d{2}", child.name):
                found[int(child.name)] = child
        return found
    outputs = root / "outputs"
    if outputs.is_dir():
        for child in sorted(outputs.iterdir()):
            m = re.fullmatch(rf"{re.escape(slug)}_ep(\d{{2}})_.+", child.name)
            if m and child.is_dir() and not child.name.endswith("__dryrun"):
                found.setdefault(int(m.group(1)), child)
    return found


def read_status(folder: Path) -> dict:
    path = Path(folder) / "status.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def write_status(folder: Path, stage: str, **extra: Any) -> None:
    data = read_status(folder)
    data.update(stage=stage, updated_utc=utc_now(), **extra)
    data.setdefault("publication_approved", False)
    write_json(Path(folder) / "status.json", data)


# ============================================================================ duration
def duration_limits(root: Path) -> dict[str, float]:
    path = root / "config" / "studio.json"
    fmt = (json.loads(path.read_text(encoding="utf-8")).get("format", {}) if path.is_file() else {})
    limits = {k: float(fmt.get(k, v)) for k, v in DEFAULT_LIMITS.items()}
    if not limits["min_seconds"] <= limits["target_min_seconds"] <= limits["target_max_seconds"] \
            <= limits["max_seconds"]:
        raise ValueError(f"config/studio.json format limits are inconsistent: {limits}")
    return limits


def estimate_runtime(story: Any, gap: float, tail: float) -> float:
    words = sum(len(s.narration.split()) for s in story.scenes)
    return words / WORDS_PER_SECOND + gap * len(story.scenes) + tail


def duration_verdict(seconds: float, limits: dict[str, float]) -> tuple[str, str]:
    """('pass'|'warn'|'fail', detail) against the hard range and the target window."""
    lo, hi = limits["min_seconds"], limits["max_seconds"]
    tlo, thi = limits["target_min_seconds"], limits["target_max_seconds"]
    detail = f"{seconds:.1f}s (allowed {lo:.0f}-{hi:.0f}s, target {tlo:.0f}-{thi:.0f}s)"
    if not lo <= seconds <= hi:
        return "fail", detail
    return ("pass" if tlo <= seconds <= thi else "warn"), detail


# ============================================================================ story commands
def unique_slug(root: Path, title: str, slug: str | None = None) -> tuple[str, str | None]:
    """A slug that no existing story uses. An explicit slug that exists is refused, never reused."""
    if slug:
        if series_path(root, slug).exists():
            raise FileExistsError(f"Story '{slug}' already exists; use `story-continue --slug {slug}` "
                                  "or choose another slug")
        return slug, None
    base = story_slugify(title)
    candidate, n = base, 2
    while series_path(root, candidate).exists():
        candidate, n = f"{base}-{n}", n + 1
    return candidate, (base if candidate != base else None)


def start_story(root: Path, title: str, episodes: int | None = None, slug: str | None = None) -> dict:
    from studio.series import create_series
    planned = episodes or 5
    if planned not in (5, 6):
        raise ValueError("A story has 5 or 6 episodes")
    chosen, clashed = unique_slug(root, title, slug)
    folder = create_series(root, chosen, story_folder=True, planned_episodes=planned, title=title)
    first = episode_path(root, chosen, 1)
    first.mkdir(parents=True)
    write_status(first, "DRAFTING", story_slug=chosen, episode_number=1)
    return {"slug": chosen, "title": title, "planned_episodes": planned, "series_dir": str(folder),
            "episode_dir": str(first),
            "note": (f"'{clashed}' already exists, so this new story is '{chosen}'." if clashed else "")}


def next_step(folder: Path) -> tuple[str, str]:
    """(stage, what to do next) for an unfinished episode folder."""
    status = read_status(folder)
    stage = status.get("stage", "DRAFTING")
    if not (folder / "story.json").is_file() or not (folder / "storyboard.json").is_file():
        return stage, "write story.json + storyboard.json (/write-episode), then check and approve-script"
    digest = sha256_file(folder / "story.json")
    if status.get("script_approved") and status.get("approved_story_sha256") != digest:
        return stage, "story.json changed after approval: re-run check and approve-script"
    return stage, {
        "DRAFTING": "run check, then approve-script",
        "SCRIPT_APPROVED": "produce-episode --dry-run, then produce-episode --confirm-spend",
        "GENERATING_MEDIA": "re-run produce-episode --confirm-spend (resumes; cached paid assets are reused)",
        "DURATION_OUT_OF_RANGE": "revise narration to land inside the duration range, approve-script, re-run",
        "QC_FAILED": "/release-review fix pass (within the remaining cap), then re-run produce-episode",
        "RENDERED_QC_PASSED": "/release-review (frames, story, voice), editorial_review.md, then approve",
        APPROVED_STAGE: "approved: run cleanup if release storage was not recorded",
    }.get(stage, "inspect status.json and resume from the last completed stage")


def story_status(root: Path, slug: str) -> dict:
    """Completed episodes, next episode, open dramatic question and output folder. No media calls."""
    bible, ledger = get_series(root, slug)
    planned = (bible.identity.planned_episodes if bible.identity else len(bible.season_outline) or None)
    last = ledger.last_approved_episode
    dirs = episode_dirs(root, slug)
    unfinished = {n: p for n, p in dirs.items() if n > last}
    complete = bool(planned) and last >= planned
    if ledger.approved_episodes:
        latest = ledger.approved_episodes[-1]
        question = "; ".join(latest.unresolved_threads) or latest.next_episode_starts or bible.season_goal
    else:
        first = next((b for b in bible.season_outline if b.number == 1), None)
        question = (first.forward_question if first and first.forward_question else bible.season_goal)
    result = {
        "slug": slug, "title": bible.title, "cast_locked": bible.cast_approved,
        "planned_episodes": planned, "completed_episodes": [e.number for e in ledger.approved_episodes],
        "next_episode": None if complete else last + 1, "story_complete": complete,
        "open_dramatic_question": question,
        "finale_question": (bible.identity.finale_question if bible.identity else bible.season_goal),
        "layout": "story-folder" if uses_story_folders(root, slug) else "legacy-outputs",
        "output_folder": str(series_path(root, slug) / "episodes" if uses_story_folders(root, slug)
                             else root / "outputs"),
        "episode_folders": {f"{n:02}": str(p) for n, p in dirs.items()},
    }
    if unfinished:
        n = min(unfinished)
        stage, todo = next_step(unfinished[n])
        result["unfinished_episode"] = {"number": n, "folder": str(unfinished[n]), "stage": stage, "next": todo}
    return result


def continue_story(root: Path, slug: str) -> dict:
    """Resume an unfinished episode, else allocate the next number, else report the story complete."""
    if not series_path(root, slug).is_dir():
        raise FileNotFoundError(f"No story '{slug}'. Start one with story-start")
    status = story_status(root, slug)
    if not status["cast_locked"]:
        folder = episode_path(root, slug, 1) if status["layout"] == "story-folder" else None
        return {"action": "resume", "number": 1, "folder": str(folder) if folder else None, "stage": "CASTING",
                "next": "write cast_proposal.json (/character-casting) and cast-approve", "status": status}
    if "unfinished_episode" in status:
        return {"action": "resume", **status["unfinished_episode"], "status": status}
    if status["story_complete"]:
        return {"action": "complete", "status": status,
                "next": f"Story complete ({status['planned_episodes']} episodes). Start a new story to continue."}
    n = status["next_episode"]
    if status["layout"] != "story-folder":
        return {"action": "new", "number": n, "folder": None, "status": status,
                "next": f"legacy layout: create outputs/{slug}_ep{n:02}_<title-slug>/ and /write-episode"}
    folder = episode_path(root, slug, n)
    folder.mkdir(parents=True, exist_ok=False)
    write_status(folder, "DRAFTING", story_slug=slug, episode_number=n)
    return {"action": "new", "number": n, "folder": str(folder), "status": status,
            "next": "write story.json + storyboard.json (/write-episode)"}


# ============================================================================ storyboard checks
def storyboard_checks(story: Any, board: dict, bible: Any, *, strict: bool, motion_seconds: int,
                      runtime_estimate: float | None = None,
                      limits: dict[str, float] | None = None) -> tuple[list[str], list[str]]:
    """Shot-plan errors/warnings. strict=True applies the v6 story-folder shot contract."""
    errors: list[str] = []
    warnings: list[str] = []
    shots = board.get("shots") or []
    if not shots:
        return ["storyboard.json has no shots"], warnings
    ids = [str(s.get("shot_id", "")) for s in shots]
    if len(set(ids)) != len(ids) or "" in ids:
        errors.append("Every shot needs a unique shot_id")
    try:
        groups = [int(str(s["scene_id"]).lstrip("Ss")) - 1 for s in shots]
    except (KeyError, ValueError):
        return errors + ["Every shot needs a scene_id like S01"], warnings
    if sorted(groups) != groups or set(groups) != set(range(len(story.scenes))):
        errors.append("Every scene needs >= 1 shot and shots must be in scene order")
    cast = {c.id for c in bible.characters}
    required_motion = 0
    for shot, scene_i in zip(shots, groups):
        sid = shot.get("shot_id", "?")
        scene = story.scenes[scene_i] if 0 <= scene_i < len(story.scenes) else None
        unknown = set(shot.get("character_ids", [])) - cast
        if unknown:
            errors.append(f"{sid}: unregistered characters {sorted(unknown)}")
        if shot.get("caption_zone") and shot["caption_zone"] not in CAPTION_ZONES:
            errors.append(f"{sid}: caption_zone must be one of {sorted(CAPTION_ZONES)}")
        if shot.get("motion_required"):
            required_motion += 1
            if not shot.get("video_prompt"):
                errors.append(f"{sid}: motion_required shots need a video_prompt describing the action")
        if not strict or scene is None:
            continue
        for key in ("shot_size", "action", "start_state", "end_state", "audio_intent", "visual_description"):
            if not str(shot.get(key, "")).strip():
                errors.append(f"{sid}: missing {key}")
        if shot.get("shot_size") and shot["shot_size"] not in SHOT_SIZES:
            warnings.append(f"{sid}: unusual shot_size {shot['shot_size']!r}")
        intent = shot.get("audio_intent")
        if intent and intent not in AUDIO_INTENTS:
            errors.append(f"{sid}: audio_intent must be one of {sorted(AUDIO_INTENTS)}")
        speaker = shot.get("speaker_id", scene.speaker_id)
        if speaker != scene.speaker_id:
            errors.append(f"{sid}: speaker_id {speaker!r} differs from its scene's voice {scene.speaker_id!r}")
        if set(shot.get("listener_ids", [])) - cast:
            errors.append(f"{sid}: listener_ids reference unregistered characters")
        if intent == "on_camera" and scene.speaker_id != "narrator":
            warnings.append(f"{sid}: on-camera dialogue without lip sync; prefer off_camera/reaction staging")
    if required_motion and motion_seconds < 3 * required_motion:
        errors.append(f"{required_motion} motion_required shots need >= {3 * required_motion}s of motion "
                      f"budget; only {motion_seconds}s is configured")
    if strict and not any(s.get("motion_required") for s in shots):
        warnings.append("No motion_required shot: mark the plot-carrying actions so they get real motion")
    if runtime_estimate is not None and limits is not None:
        # Advisory only: the authoritative gate is the measured voice track (produce-episode, before images).
        verdict, detail = duration_verdict(runtime_estimate, limits)
        if verdict != "pass":
            warnings.append(f"Estimated runtime {detail} at {WORDS_PER_SECOND} words/s; rendered speech decides")
    return errors, warnings


def story_contract_episode_errors(story: Any) -> list[str]:
    """Canon hand-off required before a story-folder episode is approved for production."""
    errors = []
    if not story.character_knowledge:
        errors.append("character_knowledge is empty: record what each character knows after this episode")
    if not story.next_episode_starts.strip():
        errors.append("next_episode_starts is empty: say where the next episode begins (or the finale payoff)")
    if not story.canon_updates:
        errors.append("canon_updates is empty: record what changed")
    return errors


# ============================================================================ scratch manifest
class UnsafePath(ValueError):
    """A manifest path that escapes the run directory, crosses a symlink or names a protected file."""


def _is_protected(rel: Path) -> bool:
    text = rel.as_posix()
    return (text in PROTECTED_FILES or any(text == d or text.startswith(d + "/") for d in PROTECTED_DIRS)
            or any(d.startswith(text + "/") for d in PROTECTED_DIRS))


def safe_relative(run_dir: Path, path: Path | str) -> Path:
    """Validate a path for the manifest: inside run_dir, no '..', no symlink anywhere on the way."""
    base = Path(run_dir).resolve(strict=True)
    raw = Path(path)
    candidate = raw if raw.is_absolute() else base / raw
    if ".." in candidate.parts:
        raise UnsafePath(f"{path}: parent references are not allowed")
    try:
        rel = candidate.relative_to(base)
    except ValueError:
        try:
            rel = candidate.absolute().relative_to(Path(run_dir).absolute())
        except ValueError:
            raise UnsafePath(f"{path}: outside the run directory {base}") from None
    if not rel.parts:
        raise UnsafePath("the run directory itself cannot be scratch")
    current = base
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafePath(f"{path}: crosses a symlink ({current})")
    if _is_protected(rel):
        raise UnsafePath(f"{path}: protected (master, script, shot plan, reports or accepted assets)")
    return rel


class ScratchManifest:
    """Per-run list of workflow-created disposable files. Only listed paths are ever cleaned."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / MANIFEST_NAME
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        self.entries: dict[str, dict] = {e["path"]: e for e in data.get("entries", [])}
        self.history: list[dict] = data.get("cleanups", [])

    def add(self, paths: list[Path | str] | Path | str, reason: str, kind: str = "file") -> None:
        if kind not in ("file", "dir"):
            raise ValueError("kind must be file or dir")
        for p in paths if isinstance(paths, list) else [paths]:
            rel = safe_relative(self.run_dir, p).as_posix()
            self.entries.setdefault(rel, {"path": rel, "kind": kind, "reason": reason, "added_utc": utc_now()})

    def save(self) -> None:
        write_json(self.path, {"about": "Workflow-created scratch for this episode run. Removed only after the "
                                         "master passed QC, release.json holds its checksum and canon is "
                                         "committed. Paid accepted sources are never listed.",
                               "run_dir": str(self.run_dir), "entries": list(self.entries.values()),
                               "cleanups": self.history})


def register_scratch(run_dir: Path, paths: list[Path | str] | Path | str, reason: str, kind: str = "file") -> None:
    manifest = ScratchManifest(run_dir)
    manifest.add(paths, reason, kind)
    manifest.save()


def tree_bytes(path: Path) -> int:
    if path.is_symlink() or not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def physical_bytes(path: Path) -> int:
    """Bytes on disk counting each hard-linked file once."""
    seen, total = set(), 0
    for p in [path] if path.is_file() else path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                total += st.st_size
    return total


# ============================================================================ release + cleanup
def probe_master(master: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate",
                          "-of", "json", str(master)], check=True, capture_output=True, text=True).stdout
    info = json.loads(out)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    num, _, den = str(v.get("avg_frame_rate", "0/1")).partition("/")
    fps = round(float(num) / float(den or 1), 3) if float(den or 1) else 0.0
    return {"duration_seconds": round(float(info.get("format", {}).get("duration", 0)), 2),
            "width": v.get("width"), "height": v.get("height"), "fps": fps,
            "video_codec": v.get("codec_name"), "audio_codec": a.get("codec_name")}


def deduplicate_retained(folder: Path) -> list[dict]:
    """Replace byte-identical retained source files with hard links to one copy (paths keep working)."""
    by_hash: dict[str, Path] = {}
    merged = []
    for rel in RETAINED_DIRS:
        base = folder / rel
        if not base.is_dir() or base.is_symlink():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            digest = sha256_file(p)
            keep = by_hash.setdefault(digest, p)
            if keep == p or os.path.samefile(keep, p):
                continue
            tmp = p.with_name(p.name + ".dedup-tmp")
            os.link(keep, tmp)
            tmp.replace(p)
            merged.append({"path": p.relative_to(folder).as_posix(), "linked_to": keep.relative_to(folder).as_posix(),
                           "bytes": p.stat().st_size})
    return merged


def retained_assets(folder: Path) -> list[dict]:
    rows = []
    for rel in RETAINED_DIRS:
        base = folder / rel
        if base.is_dir() and not base.is_symlink():
            rows += [{"path": p.relative_to(folder).as_posix(), "bytes": p.stat().st_size}
                     for p in sorted(base.rglob("*")) if p.is_file() and not p.is_symlink()]
    return rows


def register_references(root: Path, folder: Path, story: Any) -> dict:
    """Point each character / recurring prop at its first single-subject accepted still (stored once)."""
    sdir = series_path(root, story.series_slug)
    path = sdir / "references" / "manifest.json"
    if not path.parent.is_dir():
        return {}
    manifest = read_json(path) if path.is_file() else {"characters": {}, "props": {}}
    board = json.loads((folder / "storyboard.json").read_text(encoding="utf-8"))
    added = {}
    for shot in board.get("shots", []):
        image = (shot.get("actual_asset") or {}).get("image")
        if not image or not (folder / image).is_file():
            continue
        rel = (folder / image).relative_to(sdir).as_posix()
        ids = shot.get("character_ids", [])
        if len(ids) == 1 and ids[0] not in manifest["characters"]:
            manifest["characters"][ids[0]] = {"asset": rel, "episode": story.episode_number,
                                              "shot_id": shot["shot_id"], "sha256": sha256_file(folder / image)}
            added[ids[0]] = rel
        prop = shot.get("reference_prop")
        if prop and prop not in manifest["props"]:
            manifest["props"][prop] = {"asset": rel, "episode": story.episode_number, "shot_id": shot["shot_id"],
                                       "sha256": sha256_file(folder / image)}
            added[f"prop:{prop}"] = rel
    write_json(path, manifest)
    return added


def write_release(root: Path, folder: Path, story: Any, reviewer: str) -> dict:
    """Compact release record: QC, rights/provenance, provider jobs, costs and the master checksum."""
    master = folder / "master_9x16.mp4"
    reports = folder / "reports"
    quality = read_json(reports / "quality_report.json") if (reports / "quality_report.json").is_file() else {}
    generation = read_json(reports / "generation_report.json") if (reports / "generation_report.json").is_file() \
        else {}
    jobs = read_json(reports / "jobs.json") if (reports / "jobs.json").is_file() else {}
    by_status: dict[str, int] = {}
    for job in jobs.values():
        by_status[job.get("status", "?")] = by_status.get(job.get("status", "?"), 0) + 1
    bible, _ = get_series(root, story.series_slug)
    checks = quality.get("checks", [])
    record = {
        "story_slug": story.series_slug, "episode_number": story.episode_number,
        "planned_episodes": bible.identity.planned_episodes if bible.identity else len(bible.season_outline),
        "title": story.title, "approved_utc": utc_now(), "reviewer": reviewer,
        "master": {"path": master.name, "sha256": sha256_file(master), "bytes": master.stat().st_size,
                   **probe_master(master)},
        "qc": {"passed": quality.get("passed"), "report": "reports/quality_report.json",
               "failed": [c["name"] for c in checks if c.get("status") == "fail"],
               "warnings": [f"{c['name']}: {c.get('detail', '')}" for c in checks if c.get("status") == "warn"]},
        "editorial_review": "reports/editorial_review.md",
        "rights_provenance": {"assets": generation.get("asset_provenance", ""),
                              "music": generation.get("music", {"source": "none"}),
                              "originality_notes": story.originality_notes,
                              "publication": "manual upload by the owner; set platform AI disclosure at upload"},
        "provider_jobs": {"record": "reports/jobs.json", "count": len(jobs), "by_status": by_status},
        "costs": {"record": "reports/cost_report.json",
                  "fal_usd_estimated": generation.get("fal_usd_estimated_spend"),
                  "elevenlabs_characters": generation.get("elevenlabs_characters_sent")},
        "consequence_for_next": story.next_episode_starts or story.ending_payoff,
    }
    record["deduplicated"] = deduplicate_retained(folder)
    record["retained_assets"] = retained_assets(folder)
    record["references_registered"] = register_references(root, folder, story)
    write_json(folder / RELEASE_NAME, record)
    return record


def cleanup_episode(root: Path, folder: Path) -> dict:
    """Remove only manifest-listed scratch, only after QC + release record + committed canon. Idempotent."""
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise UnsafePath(f"{folder} is not a real directory")
    status = read_status(folder)
    if status.get("stage") != APPROVED_STAGE or not status.get("publication_approved"):
        raise ValueError("Cleanup runs only after approve (QC passed, review recorded, canon committed)")
    release_path = folder / RELEASE_NAME
    if not release_path.is_file():
        raise ValueError("release.json is missing; run approve first")
    release = read_json(release_path)
    master = folder / "master_9x16.mp4"
    if not master.is_file() or sha256_file(master) != release["master"]["sha256"]:
        raise ValueError("Master checksum does not match release.json; nothing removed")
    _, ledger = get_series(root, release["story_slug"])
    if release["episode_number"] not in [e.number for e in ledger.approved_episodes]:
        raise ValueError("Canon is not committed for this episode; nothing removed")
    manifest = ScratchManifest(folder)
    before = physical_bytes(folder)
    removed, skipped, reclaimed = [], [], 0
    for rel, entry in list(manifest.entries.items()):
        try:
            safe = safe_relative(folder, rel)
        except UnsafePath as exc:
            skipped.append({"path": rel, "reason": str(exc)})
            continue
        target = folder / safe
        if not target.exists() and not target.is_symlink():
            continue  # already cleaned: repeat runs are harmless
        if target.is_dir():
            inner = list(target.rglob("*"))
            if any(p.is_symlink() for p in inner):
                skipped.append({"path": rel, "reason": "directory contains a symlink"})
                continue
            if any(_is_protected(p.relative_to(folder)) for p in inner):
                skipped.append({"path": rel, "reason": "directory contains protected files"})
                continue
            size = tree_bytes(target)
            shutil.rmtree(target)
        else:
            size = target.stat().st_size
            target.unlink()
        reclaimed += size
        removed.append(rel)
        entry["removed_utc"] = utc_now()
        parent = target.parent
        while parent != folder and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    after = physical_bytes(folder)
    summary = {"removed": len(removed), "skipped": skipped, "reclaimed_bytes": before - after,
               "listed_bytes_removed": reclaimed, "retained_bytes": after, "at_utc": utc_now()}
    if removed or not manifest.history:
        manifest.history.append(summary)
    manifest.save()
    release["storage"] = {"retained_bytes": after,
                          "reclaimed_bytes": sum(h.get("reclaimed_bytes", 0) for h in manifest.history),
                          "master_bytes": release["master"]["bytes"], "manifest": MANIFEST_NAME}
    write_json(release_path, release)
    return summary


# ============================================================================ review helpers
def review_sheets(folder: Path, frames: int = 8) -> dict:
    """Contact sheets of the stills and of `frames` evenly spaced master frames, listed as scratch."""
    folder = Path(folder)
    master = folder / "master_9x16.mp4"
    out = folder / "work" / "review"
    out.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    duration = probe_master(master)["duration_seconds"]
    for k in range(frames):
        t = duration * (k + 0.5) / frames
        dest = out / f"frame_{k + 1:02}.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(master),
                        "-frames:v", "1", "-vf", "scale=360:-2", str(dest)], check=True)
        made.append(dest)
    cols = 4
    rows = -(-frames // cols)
    sheet = out / "frames_sheet.png"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(out / "frame_%02d.png"),
                    "-vf", f"tile={cols}x{rows}:padding=6", "-frames:v", "1", str(sheet)], check=True)
    made.append(sheet)
    stills = sorted((folder / "visuals" / "images").glob("*.png"))
    if stills:
        listing = out / "stills.txt"
        listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in stills), encoding="utf-8")
        still_sheet = out / "stills_sheet.png"
        rows = -(-len(stills) // 6)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-vf", f"scale=240:-2,tile=6x{rows}:padding=4", "-frames:v", "1",
                        str(still_sheet)], check=True)
        made += [listing, still_sheet]
    register_scratch(folder, made, "review frames / contact sheet")
    return {"frames_sheet": str(sheet), "stills_sheet": str(out / "stills_sheet.png") if stills else None,
            "frames": [str(p) for p in made if p.name.startswith("frame_")]}


def reject_take(folder: Path, shot_id: str) -> dict:
    """Move a shot's accepted still/clip to visuals/rejected (scratch) and retire its jobs, so the next
    produce-episode run regenerates only that shot (within the remaining cap)."""
    from studio.providers import JobStore
    folder = Path(folder)
    if not re.fullmatch(r"S\d{2}-[A-Z0-9]+", shot_id):
        raise ValueError("shot_id looks like S03-B")
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    moved = []
    for sub, ext in (("images", ".png"), ("video_clips", ".mp4")):
        src = folder / "visuals" / sub / f"{shot_id}{ext}"
        if src.is_file() and not src.is_symlink():
            dest = folder / "visuals" / "rejected" / f"{shot_id}_{sub}_{stamp}{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dest)
            moved.append(dest)
    if not moved:
        raise FileNotFoundError(f"No accepted still or clip for {shot_id}")
    store = JobStore(folder / "reports" / "jobs.json")
    retired = store.retire(f"image:{shot_id}", exact=True) + store.retire(f"clip:{shot_id}:")
    register_scratch(folder, moved, f"rejected take of {shot_id}")
    return {"moved": [p.relative_to(folder).as_posix() for p in moved], "jobs_retired": retired}
