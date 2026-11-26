#!/usr/bin/env python3
"""Promo Shorts queue: one ad image -> one YouTube Short, every 12 hours.

Owner-authorized 2026-11-25 for www.pilotmycareer.com; publishing as public owner-authorized
2026-11-26 (config "privacy": "public", this queue only). It reuses the story queue's
uploader (studio/youtube_upload.py): channel pinning, OS-keychain OAuth, duplicate-proof
ledger and the private/unlisted-only guard. It keeps its OWN config, ledger, halt file,
keychain entry and launchd job, so the TaleNovi story queue is not affected.

Campaigns live in promo/<slug>/campaign.json (+ images/). Each post is rendered locally
(free, ffmpeg) to promo/<slug>/videos/<slug>_postNN.mp4: a 1080x1920 30 fps H.264/AAC
Short with a gentle push-in; portrait, square and landscape art is fitted over a blurred
fill, never cropped. Posts upload in campaign order, at most one per
`min_hours_between_uploads` (12) hours.

Commands:
  build [--force]   render every post's Short (also done on demand by `run`)
  status            what is uploaded, what is next, when
  next              dry-run preview of the next upload (no sign-in)
  authorize         one-time browser sign-in for the promo channel
  run               upload the next post if one is due
  resolve           settle an unconfirmed upload (after checking YouTube Studio)
  clear-halt        resume after a halt was investigated
  schedule-install  run `run --scheduled` every hour via launchd
  schedule-remove   stop the schedule
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
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
PROMO_DIR = ROOT / "promo"
CONFIG_PATH = ROOT / "config" / "promo_youtube.json"
LEDGER_PATH = ROOT / "state" / "promo_youtube_ledger.json"
HALT_PATH = ROOT / "state" / "promo_queue.HALT"
ALERT_STATE = ROOT / "state" / "promo_queue_alerts.json"
LOG_PATH = ROOT / "logs" / "promo_queue.log"
LAUNCH_LABEL = "com.talenovi.promo-queue"
LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
MUSIC_PROVENANCE = ROOT / "assets" / "music" / "provenance.json"
W, H, FPS = 1080, 1920, 30

log = logging.getLogger("promo_queue")


# --------------------------------------------------------------------------- config / campaigns

def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = {
        "expected_channel_id": None,
        "category_id": "27",                 # Education
        "contains_synthetic_media": True,    # the ad art shows AI-generated, realistic people
        "made_for_kids": False,
        "min_hours_between_uploads": 12,
        "privacy": "unlisted",
        "keyring_user": "promo-oauth-credentials",
        "campaigns": ["pilotmycareer"],
    }
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    if cfg.get("keyring_user") == yt.KEYRING_USER and cfg.get("expected_channel_id") != yt.load_config().get(
            "expected_channel_id"):
        raise yt.UploadError("config/promo_youtube.json shares the story queue's sign-in but pins a different "
                             "channel; remove keyring_user or pin the same channel.")
    return cfg


PROMO_PRIVACY = ("private", "unlisted", "public")


def promo_privacy(cfg: dict) -> str:
    privacy = cfg.get("privacy", "unlisted")
    if privacy not in PROMO_PRIVACY:
        raise yt.UploadError(f"config/promo_youtube.json privacy {privacy!r}: use one of {PROMO_PRIVACY}.")
    return privacy


def require_channel(cfg: dict) -> str:
    ch = cfg.get("expected_channel_id") or ""
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", ch):
        raise yt.UploadError("Set expected_channel_id in config/promo_youtube.json to the channel these Shorts "
                             "go to (YouTube Studio > Settings > Channel > Advanced settings).")
    return ch


@dataclass(frozen=True)
class Post:
    campaign: dict
    data: dict

    @property
    def id(self) -> int:
        return int(self.data["id"])

    @property
    def slug(self) -> str:
        return self.campaign["slug"]

    @property
    def ledger_slug(self) -> str:
        return f"promo-{self.slug}"

    @property
    def image(self) -> Path:
        return safe_path(self.campaign["_dir"], self.data["image"])

    @property
    def video(self) -> Path:
        return self.campaign["_dir"] / "videos" / f"{self.slug.replace('-', '_')}_post{self.id:02d}.mp4"


def safe_path(base: Path, rel: str) -> Path:
    p = (base / rel).resolve()
    if base.resolve() not in p.parents:
        raise yt.UploadError(f"{rel!r} points outside {base}.")
    return p


def load_campaign(slug: str, promo_dir: Path = PROMO_DIR) -> dict:
    d = promo_dir / slug
    path = d / "campaign.json"
    if not path.is_file():
        raise yt.UploadError(f"No campaign file at {path}.")
    c = json.loads(path.read_text())
    if c.get("slug") != slug:
        raise yt.UploadError(f"{path}: slug must be {slug!r}.")
    ids = [p.get("id") for p in c.get("posts", [])]
    if not ids or any(not isinstance(i, int) or i < 1 for i in ids) or len(set(ids)) != len(ids):
        raise yt.UploadError(f"{path}: every post needs a unique positive integer id.")
    c["_dir"] = d
    return c


def posts(campaigns: list[dict]) -> list[Post]:
    return [Post(c, p) for c in campaigns for p in c["posts"]]


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for t in items:
        k = re.sub(r"\s+", "", str(t)).lstrip("#").casefold()
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


def make_plan(post: Post, cfg: dict) -> yt.UploadPlan:
    c, p = post.campaign, post.data
    title = p["title"].strip()
    yt._check_text("title", title)
    if not title or len(title) > yt.MAX_TITLE_CHARS:
        raise yt.UploadError(f"Post {post.id}: title must be 1-{yt.MAX_TITLE_CHARS} characters ({len(title)}).")
    hashtags = yt.normalize_hashtags(_dedupe([*c.get("base_hashtags", []), *p.get("hashtags", [])]))
    description = c["description_template"].format(
        hook=p["hook"].strip(), audience=p.get("audience", "").strip(), site=c["site"],
        hashtags=" ".join(hashtags)).strip()
    description = re.sub(r"\n{3,}", "\n\n", description)
    yt._check_text("description", description)
    if len(description.encode()) > yt.MAX_DESCRIPTION_BYTES:
        raise yt.UploadError(f"Post {post.id}: description exceeds {yt.MAX_DESCRIPTION_BYTES} bytes.")
    tags = yt.check_tags(_dedupe([*c.get("base_tags", []), *p.get("tags", [])]))
    return yt.UploadPlan(
        video=post.video, story=c["campaign"], story_slug=post.ledger_slug, episode=post.id, title=title,
        description=description, category_id=str(cfg["category_id"]),
        contains_synthetic_media=bool(cfg["contains_synthetic_media"]), privacy=promo_privacy(cfg),
        tags=tags, made_for_kids=cfg.get("made_for_kids"), public_allowed=True)


# --------------------------------------------------------------------------- render (local, free)

def ffmpeg_bin() -> str:
    for cand in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if cand and Path(cand).exists():
            return cand
    raise yt.UploadError("ffmpeg not found (brew install ffmpeg).")


def music_file(campaign: dict, provenance: Path = MUSIC_PROVENANCE) -> Path | None:
    name = (campaign.get("video") or {}).get("audio_file")
    if not name:
        return None
    tracks = json.loads(provenance.read_text()).get("tracks", []) if provenance.exists() else []
    track = next((t for t in tracks if t.get("file") == name), None)
    if not track or not track.get("commercial_use_confirmed") or "youtube" not in track.get("platforms_confirmed", []):
        raise yt.UploadError(f"audio_file {name!r} is not listed in {provenance.name} with commercial use and "
                             "YouTube confirmed; not using it.")
    return safe_path(provenance.parent, name)


def render_args(image: Path, out: Path, seconds: float, zoom: float, music: Path | None, ff: str) -> list[str]:
    frames = int(round(seconds * FPS))
    vf = (f"[0:v]format=rgb24,split[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=40:2,"
          f"eq=brightness=-0.08:saturation=0.9[bg];"
          f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,scale={2 * W}:{2 * H}:flags=lanczos,"
          f"zoompan=z='1+{zoom}*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
          f"format=yuv420p,setsar=1[v]")
    if music:
        audio_in = ["-i", str(music)]
        af = (f";[1:a]atrim=0:{seconds},asetpts=PTS-STARTPTS,afade=t=in:d=0.3,"
              f"afade=t=out:st={max(0.0, seconds - 1.0)}:d=1,loudnorm=I=-14:TP=-1.5,aresample=48000[aud]")
    else:
        audio_in = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        af = f";[1:a]atrim=0:{seconds}[aud]"
    return [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(image), *audio_in,
            "-filter_complex", vf + af, "-map", "[v]", "-map", "[aud]", "-t", f"{seconds}",
            "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-movflags", "+faststart", str(out)]


def build(post: Post, *, force: bool = False, runner=subprocess.run) -> Path:
    """Render one post's Short if missing (or --force). Verifies the source image first."""
    img = post.image
    if not img.is_file():
        raise yt.UploadError(f"Post {post.id}: image not found: {img}")
    want = post.data.get("image_sha256")
    if want and yt.sha256_file(img) != want:
        raise yt.UploadError(f"Post {post.id}: {img.name} changed since it was added (SHA-256 differs); "
                             "not using it.")
    if post.video.exists() and not force:
        return post.video
    v = post.campaign.get("video") or {}
    post.video.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=post.video.parent, prefix=".render-", suffix=".mp4")
    os.close(fd)
    try:
        r = runner(render_args(img, Path(tmp), float(v.get("seconds", 10)), float(v.get("zoom", 0.04)),
                               music_file(post.campaign), ffmpeg_bin()), capture_output=True, text=True)
        if r.returncode != 0:
            raise yt.UploadError(f"Post {post.id}: ffmpeg failed: {(r.stderr or '').strip()[-400:]}")
        os.replace(tmp, post.video)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    log.info("Rendered post %02d -> %s", post.id, post.video)
    return post.video


# --------------------------------------------------------------------------- selection

@dataclass
class Decision:
    action: str            # "upload" | "wait" | "idle" | "halt"
    reason: str
    post: Post | None = None
    due_at: datetime | None = None


def decide(ledger: yt.Ledger, all_posts: list[Post], cfg: dict, now: datetime,
           halt_path: Path = HALT_PATH, ignore_interval: bool = False) -> Decision:
    if halt_path.exists():
        return Decision("halt", "Promo queue is halted: " + halt_path.read_text().strip())
    pending = ledger.pending()
    if pending:
        p = pending[0]
        return Decision("halt", f"Unconfirmed upload of {p.get('story_slug')} post {p.get('episode')} "
                                f"(started {p.get('started_at')}). Check YouTube Studio, then run `resolve`.")
    nxt = next((p for p in all_posts if not ledger.by_story_episode(p.ledger_slug, p.id)), None)
    if nxt is None:
        return Decision("idle", "Nothing to upload: every promo post is uploaded. Add posts to the campaign.")
    last = ledger.last_done_time()
    hours = float(cfg.get("min_hours_between_uploads", 12))
    if last and not ignore_interval:
        due = last + timedelta(hours=hours)
        if now < due:
            return Decision("wait", f"Next upload ({nxt.slug} post {nxt.id:02d}) is due at "
                                    f"{due.astimezone():%Y-%m-%d %H:%M}.", nxt, due)
    return Decision("upload", f"{nxt.slug} post {nxt.id:02d} is due now.", nxt)


# --------------------------------------------------------------------------- side effects

def notify(message: str, title: str = "PilotMyCareer Shorts uploader") -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                       check=False, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def alert_throttled(key: str, message: str, every_hours: float = 12) -> None:
    state = json.loads(ALERT_STATE.read_text()) if ALERT_STATE.exists() else {}
    now = datetime.now(timezone.utc)
    last = state.get(key)
    if last and now - datetime.fromisoformat(last) < timedelta(hours=every_hours):
        return
    state[key] = now.isoformat()
    ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE.write_text(json.dumps(state, indent=2))
    notify(message)


def halt(reason: str) -> None:
    HALT_PATH.parent.mkdir(parents=True, exist_ok=True)
    HALT_PATH.write_text(f"{yt.now_utc()} {reason}\n")
    log.error("HALTED: %s", reason)
    notify("Promo upload queue halted: " + reason[:180])


def setup_logging(verbose: bool) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH)]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


# --------------------------------------------------------------------------- commands

def load_state() -> tuple[yt.Ledger, dict, list[Post]]:
    cfg = load_config()
    return yt.Ledger(LEDGER_PATH), cfg, posts([load_campaign(s) for s in cfg["campaigns"]])


def cmd_build(args) -> int:
    _, _, all_posts = load_state()
    for p in all_posts:
        build(p, force=args.force)
        print(f"post {p.id:02d}: {p.video.relative_to(ROOT)}")
    return 0


def cmd_run(args) -> int:
    try:
        with yt.upload_lock():   # shared with the story queue: never two uploads at once
            return _run_locked(args)
    except yt.UploadError as exc:
        if "already running" not in str(exc):
            raise
        log.info("%s", exc)
        return 0


def _run_locked(args) -> int:
    ledger, cfg, all_posts = load_state()
    d = decide(ledger, all_posts, cfg, datetime.now(timezone.utc), ignore_interval=args.ignore_interval)
    log.info("%s: %s", d.action, d.reason)
    if d.action == "halt":
        alert_throttled("halt", d.reason) if args.scheduled else print(d.reason)
        return 5
    if d.action != "upload":
        if not args.scheduled:
            print(d.reason)
        return 0
    post = d.post
    try:
        require_channel(cfg)
        build(post)
        plan = make_plan(post, cfg)
        log.info("Title: %s", plan.title)
        result = yt.upload(plan, interactive=not args.scheduled, config=cfg, ledger_path=LEDGER_PATH,
                           take_lock=False, keyring_user=cfg["keyring_user"])
    except yt.NotAuthorized as exc:
        log.error("%s", exc)
        alert_throttled("auth", "YouTube authorization needed: run promo_queue.py authorize")
        return 6
    except yt.DuplicateUpload as exc:
        halt(f"Duplicate protection tripped for post {post.id}: {exc}")
        return 4
    except yt.WrongChannel as exc:
        halt(str(exc))
        return 7
    except yt.UploadError as exc:
        log.error("Upload failed: %s", exc)
        if yt.Ledger(LEDGER_PATH).pending():
            halt(f"Upload of post {post.id} is unconfirmed: {exc}")
        else:
            alert_throttled("upload_failed", f"Promo post {post.id} failed: {str(exc)[:120]}", 6)
        return 2
    log.info("DONE post %02d -> %s (privacy=%s, channel=%s)", post.id, result.url, result.privacy, result.channel_id)
    if result.warnings:
        halt(" ".join(result.warnings) + f" Video: {result.url}")
        return 3
    msg = f"PilotMyCareer post {post.id} uploaded as {result.privacy.upper()}: {result.url}"
    print(msg)
    notify(msg)
    return 0


def cmd_next(args) -> int:
    ledger, cfg, all_posts = load_state()
    d = decide(ledger, all_posts, cfg, datetime.now(timezone.utc), ignore_interval=True)
    if d.action != "upload":
        print(d.reason)
        return 0
    build(d.post)
    print("DRY RUN of the next promo upload (no sign-in, nothing sent):\n")
    print(make_plan(d.post, cfg).preview())
    real = decide(ledger, all_posts, cfg, datetime.now(timezone.utc))
    print("\n" + ("Due now." if real.action == "upload" else real.reason))
    return 0


def cmd_status(args) -> int:
    ledger, cfg, all_posts = load_state()
    try:
        authed = yt._keyring().get_password(yt.KEYRING_SERVICE, cfg["keyring_user"])
    except Exception:  # noqa: BLE001 - no keyring backend (e.g. not on the Mac)
        authed = None
    print(f"Authorized:  {'yes' if authed else 'no (run: promo_queue.py authorize)'}")
    print(f"Channel:     {cfg.get('expected_channel_id') or 'NOT SET (config/promo_youtube.json)'}")
    print(f"Visibility:  {cfg.get('privacy')}")
    print(f"Schedule:    {'installed (hourly check, max one upload per ' + str(cfg['min_hours_between_uploads']) + ' h)' if LAUNCH_PLIST.exists() else 'not installed'}")
    for p in all_posts:
        e = ledger.by_story_episode(p.ledger_slug, p.id)
        state = (f"uploaded {e['uploaded_at']}  https://youtu.be/{e['video_id']}  [{e.get('privacy_returned')}]"
                 if e and e["status"] == "done" else "UNCONFIRMED (needs resolve)" if e else
                 "waiting (rendered)" if p.video.exists() else "waiting")
        print(f"  {p.slug} post {p.id:02d}: {state}  - {p.data['title']}")
    print(f"\nNext: {decide(ledger, all_posts, cfg, datetime.now(timezone.utc)).reason}")
    return 0


def cmd_authorize(args) -> int:
    cfg = load_config()
    ch = require_channel(cfg)
    yt.forget_credentials(cfg["keyring_user"])
    yt.authorize(expected_channel=ch, keyring_user=cfg["keyring_user"])
    print("Done. The scheduled promo job can now upload without opening a browser.")
    return 0


def cmd_resolve(args) -> int:
    with yt.upload_lock():
        ledger = yt.Ledger(LEDGER_PATH)
        slug = f"promo-{args.campaign}"
        matches = [e for e in ledger.pending() if e.get("story_slug") == slug and e.get("episode") == args.post]
        if not matches:
            print("No unconfirmed upload matches that campaign and post.")
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
        print(f"Recorded {args.campaign} post {args.post} as {entry['status']}.")
    return 0


def cmd_clear_halt(args) -> int:
    if HALT_PATH.exists():
        print("Clearing halt: " + HALT_PATH.read_text().strip())
        HALT_PATH.unlink()
    else:
        print("Promo queue is not halted.")
    return 0


def cmd_schedule_install(args) -> int:
    require_channel(load_config())
    plist = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [str(_VENV_PY), str(Path(__file__).resolve()), "run", "--scheduled"],
        "WorkingDirectory": str(ROOT),
        "StartInterval": 3600,
        "RunAtLoad": True,
        "StandardOutPath": str(ROOT / "logs" / "promo_queue.launchd.log"),
        "StandardErrorPath": str(ROOT / "logs" / "promo_queue.launchd.log"),
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
    hours = load_config()["min_hours_between_uploads"]
    print(f"Installed {LAUNCH_PLIST}: checks every hour, uploads at most once per {hours} h.")
    return 0


def cmd_schedule_remove(args) -> int:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"], capture_output=True)
    if LAUNCH_PLIST.exists():
        LAUNCH_PLIST.unlink()
        print("Promo schedule removed.")
    else:
        print("No promo schedule was installed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--force", action="store_true", help="Re-render even if the video exists")
    r = sub.add_parser("run")
    r.add_argument("--scheduled", action="store_true", help="Background mode: never open a browser")
    r.add_argument("--ignore-interval", action="store_true", help="Owner only: skip the 12-hour wait once")
    sub.add_parser("next")
    sub.add_parser("status")
    sub.add_parser("authorize")
    rs = sub.add_parser("resolve")
    rs.add_argument("--campaign", default="pilotmycareer")
    rs.add_argument("--post", required=True, type=int)
    g = rs.add_mutually_exclusive_group(required=True)
    g.add_argument("--video-id", help="It IS on YouTube: its 11-character video ID")
    g.add_argument("--not-uploaded", action="store_true", help="It is NOT on YouTube; allow a retry")
    sub.add_parser("clear-halt")
    sub.add_parser("schedule-install")
    sub.add_parser("schedule-remove")
    args = p.parse_args(argv)
    setup_logging(verbose=not getattr(args, "scheduled", False))
    handlers = {"build": cmd_build, "run": cmd_run, "next": cmd_next, "status": cmd_status,
                "authorize": cmd_authorize, "resolve": cmd_resolve, "clear-halt": cmd_clear_halt,
                "schedule-install": cmd_schedule_install, "schedule-remove": cmd_schedule_remove}
    try:
        return handlers[args.cmd](args)
    except yt.UploadError as exc:
        log.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
