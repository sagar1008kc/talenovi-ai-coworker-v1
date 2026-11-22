#!/usr/bin/env python3
"""Daily YouTube upload queue for uploads/<story>/<story>_episodeNN.mp4.

Rules (deterministic, no guessing):
  * One upload per run, and only if the last confirmed upload was at least
    `min_hours_between_uploads` (24) hours ago.
  * Stories are taken in the order they first appeared in uploads/ (remembered
    in the ledger). Within a story, episodes go strictly in order 01, 02, 03 ...
    An episode is never skipped: if the next one is missing, that story waits
    and the queue moves on to the next story that has its next episode ready.
  * Nothing is uploaded twice: the ledger blocks by file SHA-256 and by
    story + episode. An unconfirmed upload, a channel mismatch or a video that
    YouTube did not keep unlisted halts the whole queue until resolved.

Commands:
  status            what is uploaded, what is next, when
  next              dry-run preview of the next upload (no sign-in)
  authorize         one-time browser sign-in (token stored in the macOS Keychain)
  run               upload the next episode if one is due
  resolve           settle an unconfirmed upload (after checking YouTube Studio)
  clear-halt        resume after a halt was investigated
  schedule-install  run `run --scheduled` every hour via launchd (uploads at most once per 24 h)
  schedule-remove   stop the schedule
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_VENV_PY = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
try:
    import googleapiclient  # noqa: F401
except ImportError:
    if _VENV_PY.exists() and Path(sys.prefix).resolve() != _VENV_PY.parent.parent.resolve():
        os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise

from studio import youtube_upload as yt  # noqa: E402

ROOT = yt.ROOT
UPLOADS_DIR = ROOT / "uploads"
SERIES_DIR = ROOT / "series"
SHOWS_DIR = ROOT / "shows"          # director.py shows (motion-first pipeline)
LOG_PATH = ROOT / "logs" / "youtube_queue.log"
ALERT_STATE = ROOT / "state" / "youtube_queue_alerts.json"
LAUNCH_LABEL = "com.talenovi.youtube-queue"
LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
FILE_RE = re.compile(r"^(?P<prefix>[a-z0-9_]+)_episode(?P<num>\d{2})\.mp4$")

log = logging.getLogger("youtube_queue")


# --------------------------------------------------------------------------- discovery

def discover(uploads_dir: Path = UPLOADS_DIR) -> dict[str, dict[int, Path]]:
    """{story_slug: {episode_number: path}} for files named <slug_with_underscores>_episodeNN.mp4."""
    stories: dict[str, dict[int, Path]] = {}
    if not uploads_dir.is_dir():
        return stories
    for folder in sorted(p for p in uploads_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        expected_prefix = folder.name.replace("-", "_")
        eps: dict[int, Path] = {}
        for f in sorted(folder.iterdir()):
            m = FILE_RE.match(f.name)
            if not m or not f.is_file():
                continue
            if m["prefix"] != expected_prefix:
                log.warning("Ignoring %s: name should start with %s_episode", f, expected_prefix)
                continue
            eps[int(m["num"])] = f
        if eps:
            stories[folder.name] = eps
    return stories


def update_story_order(ledger: yt.Ledger, stories: dict[str, dict[int, Path]],
                       uploads_dir: Path = UPLOADS_DIR) -> list[str]:
    order: list[str] = ledger.data["story_order"]
    new = [s for s in stories if s not in order]

    def first_seen(slug: str) -> tuple[float, str]:
        st = (uploads_dir / slug).stat()
        return (getattr(st, "st_birthtime", st.st_mtime), slug)

    order.extend(sorted(new, key=first_seen))
    return order


# --------------------------------------------------------------------------- metadata

def camel(text: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in re.findall(r"[A-Za-z0-9]+", text))


def episode_metadata(slug: str, episode: int, video: Path, config: dict,
                     uploads_dir: Path = UPLOADS_DIR, series_dir: Path = SERIES_DIR) -> dict:
    """Story name, title, summary and hashtags for one episode, from (in priority order)
    uploads/<slug>/metadata.json, then the approved release in series/<slug>/.
    Raises UploadError rather than guessing."""
    side_path = uploads_dir / slug / "metadata.json"
    side = json.loads(side_path.read_text()) if side_path.exists() else {}
    side_ep = side.get("episodes", {}).get(f"{episode:02d}", {})

    shows_dir = series_dir.parent / "shows"
    director_ep = shows_dir / slug / "episodes" / f"ep{episode:02d}"
    if director_ep.is_dir():
        # Director shows: the release (episode QA incl. speech-to-text, editorial PASS) is mandatory.
        ep_dir, story_path = director_ep, director_ep / "shotlist.json"
        bible_path = shows_dir / slug / "show.json"
        if not (ep_dir / "release.json").exists():
            raise yt.UploadError(f"{slug} episode {episode}: not approved by director.py (no release.json); "
                                 "not uploading.")
    else:
        ep_dir = series_dir / slug / "episodes" / f"{episode:02d}"
        story_path = ep_dir / "story.json"
        bible_path = series_dir / slug / "series_bible.json"
    release_path = ep_dir / "release.json"
    release = json.loads(release_path.read_text()) if release_path.exists() else None
    story = json.loads(story_path.read_text()) if story_path.exists() else {}
    bible = json.loads(bible_path.read_text()) if bible_path.exists() else {}
    if director_ep.is_dir():
        story = {"summary": story.get("logline"), "youtube_description": story.get("youtube_description"),
                 "tags": story.get("tags", [])}

    if release is not None:
        if not release.get("qc", {}).get("passed"):
            raise yt.UploadError(f"{slug} episode {episode}: release QC did not pass; not uploading.")
        expected_sha = release.get("master", {}).get("sha256")
        if expected_sha and yt.sha256_file(video) != expected_sha:
            raise yt.UploadError(f"{video.name} does not match the approved master for {slug} episode {episode} "
                                 "(SHA-256 differs); not uploading.")
    elif not side_ep:
        raise yt.UploadError(f"{slug} episode {episode}: no approved release in series/{slug}/ and no entry in "
                             f"uploads/{slug}/metadata.json; not uploading.")

    story_name = side.get("story") or bible.get("title")
    title = side_ep.get("title") or (release or {}).get("title", "").split(" | ")[0].strip()
    summary = side_ep.get("description") or story.get("youtube_description") or story.get("summary")
    if not (story_name and title and summary):
        raise yt.UploadError(f"{slug} episode {episode}: story name, title or summary is missing.")

    if "hashtags" in side_ep:
        tags = list(side_ep["hashtags"])
    else:
        tags = [*config.get("default_hashtags", []), *side.get("hashtags", []), camel(story_name),
                f"Episode{episode}", *(camel(t) for t in story.get("tags", []))]
    tags = yt.normalize_hashtags(_dedupe(tags)[: yt.MAX_HASHTAGS])
    return {"story": story_name, "title": title, "summary": summary, "hashtags": tags}


def _dedupe(tags: list[str]) -> list[str]:
    seen, out = set(), []
    for t in tags:
        k = str(t).lstrip("#").casefold()
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


# --------------------------------------------------------------------------- selection

@dataclass
class Decision:
    action: str            # "upload" | "wait" | "idle" | "halt"
    reason: str
    slug: str | None = None
    episode: int | None = None
    video: Path | None = None
    due_at: datetime | None = None


def decide(ledger: yt.Ledger, stories: dict[str, dict[int, Path]], order: list[str], config: dict,
           now: datetime, halt_path: Path = yt.HALT_PATH, ignore_interval: bool = False) -> Decision:
    if halt_path.exists():
        return Decision("halt", "Queue is halted: " + halt_path.read_text().strip())
    pending = ledger.pending()
    if pending:
        p = pending[0]
        return Decision("halt", f"Unconfirmed upload of {p.get('story_slug')} episode {p.get('episode')} "
                                f"(started {p.get('started_at')}). Check YouTube Studio, then run `resolve`.")

    candidate = None
    for slug in order:
        eps = stories.get(slug)
        if not eps:
            continue
        uploaded = {e["episode"] for e in ledger.uploads.values()
                    if e.get("story_slug") == slug and e["status"] == "done"}
        n = 1
        while n in uploaded:
            n += 1
        if n in eps:
            candidate = (slug, n, eps[n])
            break
    if candidate is None:
        return Decision("idle", "Nothing to upload: every story in uploads/ is uploaded up to its last "
                                "available episode.")

    slug, n, video = candidate
    last = ledger.last_done_time()
    hours = float(config.get("min_hours_between_uploads", 24))
    if last and not ignore_interval:
        due = last + timedelta(hours=hours)
        if now < due:
            return Decision("wait", f"Next upload ({slug} episode {n:02d}) is due at {due.astimezone():%Y-%m-%d %H:%M}.",
                            slug, n, video, due)
    return Decision("upload", f"{slug} episode {n:02d} is due now.", slug, n, video)


# --------------------------------------------------------------------------- side effects

def notify(message: str, title: str = "TaleNovi YouTube uploader") -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                       check=False, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def alert_throttled(key: str, message: str, every_hours: float = 12) -> None:
    """Notify about a recurring problem at most once per `every_hours`."""
    state = json.loads(ALERT_STATE.read_text()) if ALERT_STATE.exists() else {}
    last = state.get(key)
    now = datetime.now(timezone.utc)
    if last and now - datetime.fromisoformat(last) < timedelta(hours=every_hours):
        return
    state[key] = now.isoformat()
    ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE.write_text(json.dumps(state, indent=2))
    notify(message)


def halt(reason: str) -> None:
    yt.HALT_PATH.parent.mkdir(parents=True, exist_ok=True)
    yt.HALT_PATH.write_text(f"{yt.now_utc()} {reason}\n")
    log.error("HALTED: %s", reason)
    notify("Upload queue halted: " + reason[:180])


def setup_logging(verbose: bool) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH)]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


# --------------------------------------------------------------------------- commands

def load_state() -> tuple[yt.Ledger, dict, dict[str, dict[int, Path]], list[str]]:
    ledger = yt.Ledger()
    config = yt.load_config()
    stories = discover()
    order = update_story_order(ledger, stories)
    return ledger, config, stories, order


def cmd_run(args) -> int:
    try:
        with yt.upload_lock():
            return _run_locked(args)
    except yt.UploadError as exc:  # another run holds the lock
        log.info("%s", exc)
        return 0


def _run_locked(args) -> int:
    ledger, config, stories, order = load_state()
    ledger.save()  # persist the story order
    d = decide(ledger, stories, order, config, datetime.now(timezone.utc), ignore_interval=args.ignore_interval)
    log.info("%s: %s", d.action, d.reason)
    if d.action == "halt":
        if args.scheduled:
            alert_throttled("halt", d.reason)
        else:
            print(d.reason)
        return 5
    if d.action != "upload":
        return 0

    try:
        meta = episode_metadata(d.slug, d.episode, d.video, config)
        plan = yt.make_plan(d.video, meta["story"], d.episode, meta["title"], meta["summary"], meta["hashtags"],
                            story_slug=d.slug, config=config)
        log.info("Title: %s", plan.title)
        result = yt.upload(plan, interactive=not args.scheduled, config=config, take_lock=False)
    except yt.NotAuthorized as exc:
        log.error("%s", exc)
        alert_throttled("auth", "YouTube authorization needed: run youtube_queue.py authorize")
        return 6
    except yt.DuplicateUpload as exc:
        halt(f"Duplicate protection tripped for {d.video.name}: {exc}")
        return 4
    except yt.WrongChannel as exc:
        halt(str(exc))
        return 7
    except yt.UploadError as exc:
        log.error("Upload failed: %s", exc)
        if yt.Ledger().pending():
            halt(f"Upload of {d.slug} episode {d.episode} is unconfirmed: {exc}")
        else:
            alert_throttled("upload_failed", f"Upload of {d.slug} episode {d.episode} failed; will retry next hour.", 6)
        return 2

    log.info("DONE %s episode %02d -> %s (privacy=%s, channel=%s)", d.slug, d.episode, result.url,
             result.privacy, result.channel_id)
    if result.warnings:
        halt(" ".join(result.warnings) + f" Video: {result.url}")
        return 3
    msg = f"{meta['story']} episode {d.episode} uploaded as UNLISTED: {result.url}"
    print(msg)
    notify(msg)
    return 0


def cmd_next(args) -> int:
    ledger, config, stories, order = load_state()
    d = decide(ledger, stories, order, config, datetime.now(timezone.utc), ignore_interval=True)
    if d.action != "upload":
        print(d.reason)
        return 0
    meta = episode_metadata(d.slug, d.episode, d.video, config)
    plan = yt.make_plan(d.video, meta["story"], d.episode, meta["title"], meta["summary"], meta["hashtags"],
                        story_slug=d.slug, config=config)
    print("DRY RUN of the next upload (no sign-in, nothing sent):\n")
    print(plan.preview())
    real = decide(ledger, stories, order, config, datetime.now(timezone.utc))
    print("\n" + ("Due now." if real.action == "upload" else real.reason))
    return 0


def cmd_status(args) -> int:
    ledger, config, stories, order = load_state()
    kr = yt._keyring()
    print(f"Authorized:  {'yes' if kr.get_password(yt.KEYRING_SERVICE, yt.KEYRING_USER) else 'no (run: authorize)'}")
    print(f"Channel:     {config.get('expected_channel_id') or 'not pinned'}")
    print(f"Schedule:    {'installed (hourly check, max one upload per ' + str(config['min_hours_between_uploads']) + ' h)' if LAUNCH_PLIST.exists() else 'not installed'}")
    for slug in order:
        eps = stories.get(slug, {})
        done = {e["episode"]: e for e in ledger.uploads.values()
                if e.get("story_slug") == slug and e["status"] in ("done", "pending")}
        print(f"\n{slug}  ({len(eps)} episode files)")
        for n in sorted(set(eps) | set(done)):
            e = done.get(n)
            state = (f"uploaded {e['uploaded_at']}  https://youtu.be/{e['video_id']}  [{e.get('privacy_returned')}]"
                     if e and e["status"] == "done" else "UNCONFIRMED (needs resolve)" if e else "waiting")
            print(f"  episode {n:02d}: {state}")
    d = decide(ledger, stories, order, config, datetime.now(timezone.utc))
    print(f"\nNext: {d.reason}")
    return 0


def cmd_authorize(args) -> int:
    yt.forget_credentials()
    yt.authorize()
    print("Done. The scheduled job can now upload without opening a browser.")
    return 0


def cmd_resolve(args) -> int:
    with yt.upload_lock():
        ledger = yt.Ledger()
        matches = [e for e in ledger.pending() if e.get("story_slug") == args.story and e.get("episode") == args.episode]
        if not matches:
            print("No unconfirmed upload matches that story and episode.")
            return 1
        entry = matches[0]
        if args.video_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", args.video_id):
                print("That does not look like a YouTube video ID (11 characters).")
                return 1
            entry.update(status="done", video_id=args.video_id, uploaded_at=entry["started_at"],
                         privacy_returned="unknown (resolved by hand)", resolved_at=yt.now_utc())
        else:
            entry.update(status="failed", failed_at=yt.now_utc(), error="resolved by hand: not on YouTube")
        ledger.save()
        print(f"Recorded {args.story} episode {args.episode} as {entry['status']}.")
    return 0


def cmd_clear_halt(args) -> int:
    if yt.HALT_PATH.exists():
        print("Clearing halt: " + yt.HALT_PATH.read_text().strip())
        yt.HALT_PATH.unlink()
    else:
        print("Queue is not halted.")
    return 0


def cmd_schedule_install(args) -> int:
    plist = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [str(_VENV_PY), str(Path(__file__).resolve()), "run", "--scheduled"],
        "WorkingDirectory": str(ROOT),
        "StartInterval": 3600,
        "RunAtLoad": True,
        "StandardOutPath": str(ROOT / "logs" / "youtube_queue.launchd.log"),
        "StandardErrorPath": str(ROOT / "logs" / "youtube_queue.launchd.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"},
    }
    LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"], capture_output=True)
    with open(LAUNCH_PLIST, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}")
        return 1
    print(f"Installed {LAUNCH_PLIST}: checks every hour, uploads at most once per 24 h.")
    return 0


def cmd_schedule_remove(args) -> int:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"], capture_output=True)
    if LAUNCH_PLIST.exists():
        LAUNCH_PLIST.unlink()
        print("Schedule removed.")
    else:
        print("No schedule was installed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--scheduled", action="store_true", help="Background mode: never open a browser")
    r.add_argument("--ignore-interval", action="store_true", help="Owner only: skip the 24-hour wait once")
    sub.add_parser("next")
    sub.add_parser("status")
    sub.add_parser("authorize")
    rs = sub.add_parser("resolve")
    rs.add_argument("--story", required=True)
    rs.add_argument("--episode", required=True, type=int)
    g = rs.add_mutually_exclusive_group(required=True)
    g.add_argument("--video-id", help="It IS on YouTube: its 11-character video ID")
    g.add_argument("--not-uploaded", action="store_true", help="It is NOT on YouTube; allow a retry")
    sub.add_parser("clear-halt")
    sub.add_parser("schedule-install")
    sub.add_parser("schedule-remove")
    args = p.parse_args(argv)
    setup_logging(verbose=getattr(args, "scheduled", False) is False)
    handlers = {"run": cmd_run, "next": cmd_next, "status": cmd_status, "authorize": cmd_authorize,
                "resolve": cmd_resolve, "clear-halt": cmd_clear_halt, "schedule-install": cmd_schedule_install,
                "schedule-remove": cmd_schedule_remove}
    try:
        return handlers[args.cmd](args)
    except yt.UploadError as exc:
        log.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
