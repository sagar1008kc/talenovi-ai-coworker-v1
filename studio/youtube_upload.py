"""YouTube upload core: metadata formatting, OS-keychain OAuth, resumable upload, ledger.

Credentials never touch disk inside the project and are never printed:
the OAuth token (incl. refresh token) lives in the OS credential store via
`keyring` (macOS Keychain). The OAuth *client* file stays in git-ignored
`secrets/`.

Duplicate protection: every upload is recorded in `state/youtube_ledger.json`,
keyed by the video file's SHA-256. A "pending" entry is written *before* the
upload starts and turned into "done" (with the video ID) after YouTube
confirms. A pending entry left behind by a crash blocks re-uploading that file
until it is resolved by hand, so a crash can never cause a duplicate.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import re
import tempfile
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

ROOT = Path(__file__).resolve().parent.parent
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Read-only, used only to confirm WHICH channel the sign-in belongs to before any upload.
    "https://www.googleapis.com/auth/youtube.readonly",
]
KEYRING_SERVICE = "talenovi-youtube-uploader"
KEYRING_USER = "oauth-credentials"
DEFAULT_CLIENT_SECRETS = ROOT / "secrets" / "youtube_client_secret.json"
CONFIG_PATH = ROOT / "config" / "youtube_upload.json"
LEDGER_PATH = ROOT / "state" / "youtube_ledger.json"
LOCK_PATH = ROOT / "state" / "youtube_upload.lock"
HALT_PATH = ROOT / "state" / "youtube_queue.HALT"
PRIVACY = "unlisted"                       # default; config "privacy" may be "private"
ALLOWED_PRIVACY = ("private", "unlisted")   # never "public": promotion stays manual in YouTube Studio

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_HASHTAGS = 15  # YouTube ignores *all* hashtags on a video with more than 15
RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8


class UploadError(RuntimeError):
    """A clear, user-facing failure."""


class NotAuthorized(UploadError):
    pass


class DuplicateUpload(UploadError):
    pass


class WrongChannel(UploadError):
    pass


# --------------------------------------------------------------------------- config

def load_config(path: Path = CONFIG_PATH) -> dict:
    defaults = {
        "expected_channel_id": None,
        "category_id": "24",
        "contains_synthetic_media": True,
        "min_hours_between_uploads": 24,
        "default_hashtags": ["Shorts"],
    }
    if path.exists():
        defaults.update(json.loads(path.read_text()))
    return defaults


# --------------------------------------------------------------------------- metadata

def normalize_hashtags(raw: str | list[str] | None) -> list[str]:
    """'ShortStory, #Mystery,##Episode3' -> ['#ShortStory', '#Mystery', '#Episode3'].

    Leading #'s are stripped, inner whitespace removed, empties dropped and
    duplicates (case-insensitive) removed, keeping first-seen order.
    """
    if raw is None:
        return []
    parts = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = re.sub(r"\s+", "", str(part)).lstrip("#")
        if not tag:
            continue
        if "#" in tag:
            raise UploadError(f"Hashtag {part!r} contains '#' in the middle; separate tags with commas.")
        if not re.fullmatch(r"[\w]+", tag):
            raise UploadError(f"Hashtag {part!r} may only contain letters, digits and underscores.")
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append("#" + tag)
    if len(out) > MAX_HASHTAGS:
        raise UploadError(f"{len(out)} hashtags given; YouTube ignores all of them above {MAX_HASHTAGS}.")
    return out


def _check_text(field: str, value: str) -> None:
    if "<" in value or ">" in value:
        raise UploadError(f"YouTube does not allow '<' or '>' in the {field}.")


def build_title(story: str, episode: int, title: str) -> str:
    story, title = story.strip(), title.strip()
    if not story or not title:
        raise UploadError("Story name and episode title must not be empty.")
    if episode < 1:
        raise UploadError("Episode number must be 1 or higher.")
    full = f"{story} | Episode {episode} | {title}"
    _check_text("title", full)
    if len(full) > MAX_TITLE_CHARS:
        raise UploadError(f"Title is {len(full)} characters; YouTube allows {MAX_TITLE_CHARS}: {full!r}")
    return full


def build_full_story_title(story: str, title: str | None = None) -> str:
    story = story.strip()
    if not story:
        raise UploadError("Story name must not be empty.")
    full = f"{story} | Full Story" + (f" | {title.strip()}" if title and title.strip() else "")
    _check_text("title", full)
    if len(full) > MAX_TITLE_CHARS:
        raise UploadError(f"Title is {len(full)} characters; YouTube allows {MAX_TITLE_CHARS}: {full!r}")
    return full


def build_description(summary: str, hashtags: list[str]) -> str:
    summary = summary.strip()
    if not summary:
        raise UploadError("Description (episode summary) must not be empty.")
    text = summary + ("\n\n" + " ".join(hashtags) if hashtags else "")
    _check_text("description", text)
    if len(text.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise UploadError(f"Description exceeds YouTube's {MAX_DESCRIPTION_BYTES}-byte limit.")
    return text


@dataclass(frozen=True)
class UploadPlan:
    video: Path
    story: str
    story_slug: str
    episode: int
    title: str
    description: str
    category_id: str
    contains_synthetic_media: bool
    privacy: str = PRIVACY

    def body(self) -> dict:
        if self.privacy not in ALLOWED_PRIVACY:
            raise UploadError(f"privacy {self.privacy!r} is not allowed; uploads are private or unlisted only")
        status = {"privacyStatus": self.privacy}
        if self.contains_synthetic_media:
            status["containsSyntheticMedia"] = True
        return {
            "snippet": {"title": self.title, "description": self.description, "categoryId": self.category_id},
            "status": status,
        }

    def preview(self) -> str:
        size_mb = self.video.stat().st_size / 1_000_000
        return "\n".join([
            f"Video:       {self.video}  ({size_mb:.1f} MB)",
            f"Title:       {self.title}",
            f"Visibility:  {self.privacy}",
            f"AI label:    {'containsSyntheticMedia = true' if self.contains_synthetic_media else 'not set'}",
            "Description:",
            *("  | " + line for line in self.description.split("\n")),
        ])


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "story"


def make_plan(video: str | Path, story: str, episode: int, title: str, summary: str,
              hashtags: str | list[str] | None, *, story_slug: str | None = None,
              config: dict | None = None, full_story: bool = False) -> UploadPlan:
    """full_story=True: a whole-story compilation, titled "{story} | Full Story[ | {title}]",
    recorded in the ledger as episode 0 of "<slug>-full-story"."""
    cfg = config or load_config()
    path = Path(video).expanduser().resolve()
    if not path.is_file():
        raise UploadError(f"Video file not found: {path}")
    if path.suffix.lower() != ".mp4":
        raise UploadError(f"Expected an .mp4 file, got {path.name}")
    if path.stat().st_size == 0:
        raise UploadError(f"Video file is empty: {path}")
    return UploadPlan(
        video=path, story=story.strip(),
        story_slug=story_slug or (slugify(story) + ("-full-story" if full_story else "")),
        episode=0 if full_story else int(episode),
        title=build_full_story_title(story, title) if full_story else build_title(story, int(episode), title),
        description=build_description(summary, normalize_hashtags(hashtags)),
        category_id=str(cfg["category_id"]),
        contains_synthetic_media=bool(cfg["contains_synthetic_media"]),
        privacy=_privacy(cfg),
    )


def _privacy(cfg: dict) -> str:
    privacy = cfg.get("privacy", PRIVACY)
    if privacy not in ALLOWED_PRIVACY:
        raise UploadError(f"config privacy {privacy!r} not allowed; use one of {ALLOWED_PRIVACY} "
                          "(making a video public stays a manual step in YouTube Studio)")
    return privacy


# --------------------------------------------------------------------------- ledger

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Ledger:
    """Append-only record of uploads. Entries keyed by file SHA-256."""

    def __init__(self, path: Path = LEDGER_PATH):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}
        self.data.setdefault("uploads", {})
        self.data.setdefault("story_order", [])

    @property
    def uploads(self) -> dict:
        return self.data["uploads"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def by_story_episode(self, slug: str, episode: int) -> dict | None:
        for entry in self.uploads.values():
            if entry.get("story_slug") == slug and entry.get("episode") == episode and entry["status"] in ("done", "pending"):
                return entry
        return None

    def blocking_entry(self, sha: str, slug: str, episode: int) -> dict | None:
        entry = self.uploads.get(sha)
        if entry and entry["status"] in ("done", "pending"):
            return entry
        return self.by_story_episode(slug, episode)

    def last_done_time(self) -> datetime | None:
        times = [datetime.fromisoformat(e["uploaded_at"]) for e in self.uploads.values()
                 if e["status"] == "done" and e.get("uploaded_at")]
        return max(times) if times else None

    def pending(self) -> list[dict]:
        return [e for e in self.uploads.values() if e["status"] == "pending"]


@contextmanager
def upload_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UploadError("Another upload is already running.") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- auth

def _keyring():
    import keyring
    return keyring


def load_credentials(*, interactive: bool, client_secrets: Path = DEFAULT_CLIENT_SECRETS):
    """Return valid OAuth credentials from the OS keychain, refreshing them if
    needed. When none are stored and `interactive` is true, open the browser
    for consent (offline access, account/channel picker)."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    kr = _keyring()
    stored = kr.get_password(KEYRING_SERVICE, KEYRING_USER)
    creds = None
    if stored:
        try:
            info = json.loads(stored)
            # Compare against the scopes saved with the token (not the ones we ask for now):
            # a token from before the channel check lacks youtube.readonly and must be replaced.
            if set(SCOPES) <= set(info.get("scopes") or []):
                creds = Credentials.from_authorized_user_info(info, SCOPES)
        except (ValueError, KeyError):
            creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.refresh_token:
        try:
            creds.refresh(Request())
            kr.set_password(KEYRING_SERVICE, KEYRING_USER, creds.to_json())
            return creds
        except RefreshError:
            creds = None  # revoked or expired (e.g. OAuth app left in "Testing" mode: 7-day tokens)
    if not interactive:
        raise NotAuthorized(
            "YouTube is not authorized (or the stored authorization expired). "
            "Run: .venv/bin/python youtube_queue.py authorize")
    return authorize(client_secrets)


def authorized_channel(youtube) -> tuple[str, str]:
    """(channel_id, channel_title) of the channel these credentials upload to."""
    from googleapiclient.errors import HttpError
    try:
        items = youtube.channels().list(part="snippet", mine=True).execute().get("items", [])
    except HttpError as exc:
        if getattr(exc.resp, "status", None) == 403:
            raise NotAuthorized("The sign-in does not allow checking which channel it belongs to. Run "
                                "`youtube_queue.py authorize` again and tick every permission Google shows.") from None
        raise UploadError(f"Could not check the signed-in channel: {_http_reason(exc)}") from None
    if len(items) != 1:
        raise UploadError(f"Expected exactly one YouTube channel for this sign-in, found {len(items)}.")
    return items[0]["id"], items[0]["snippet"].get("title", "")


def check_channel(youtube, expected: str | None) -> tuple[str, str]:
    channel_id, title = authorized_channel(youtube)
    if expected and channel_id != expected:
        raise WrongChannel(
            f"This sign-in uploads to '{title}' ({channel_id}), not the configured channel {expected}. "
            "Run `youtube_queue.py authorize` again and pick the right channel in Google's channel chooser.")
    return channel_id, title


def authorize(client_secrets: Path = DEFAULT_CLIENT_SECRETS, expected_channel: str | None = None):
    import logging
    from google_auth_oauthlib.flow import InstalledAppFlow

    # The local redirect server logs the request line, which carries the one-time auth code.
    logging.getLogger("google_auth_oauthlib.flow").setLevel(logging.WARNING)
    if not client_secrets.is_file():
        raise UploadError(f"OAuth client file not found: {client_secrets}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    print("Opening your browser. Choose the Google account AND the YouTube channel to upload to.")
    creds = flow.run_local_server(
        port=0, open_browser=True, access_type="offline", prompt="consent select_account",
        authorization_prompt_message="If the browser did not open, visit this URL:\n{url}\n",
        success_message="TaleNovi uploader is authorized. You can close this tab.",
    )
    if not creds.refresh_token:
        raise UploadError("Google did not return offline access (no refresh token). Remove the app's access "
                          "at myaccount.google.com/permissions and authorize again.")
    from googleapiclient.discovery import build
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    expected = expected_channel if expected_channel is not None else load_config().get("expected_channel_id")
    try:
        channel_id, title = check_channel(youtube, expected)
    except UploadError:
        forget_credentials()  # never keep a sign-in for the wrong channel
        raise
    _keyring().set_password(KEYRING_SERVICE, KEYRING_USER, creds.to_json())
    print(f"Signed in to channel '{title}' ({channel_id}).")
    print("Authorization saved in the macOS Keychain (service 'talenovi-youtube-uploader').")
    return creds


def forget_credentials() -> bool:
    kr = _keyring()
    if kr.get_password(KEYRING_SERVICE, KEYRING_USER) is None:
        return False
    kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
    return True


# --------------------------------------------------------------------------- upload

def resumable_upload(request, *, progress: Callable[[float], None] | None = None,
                     sleep: Callable[[float], None] = time.sleep) -> dict:
    """Drive a resumable `videos().insert` request to completion with bounded
    exponential backoff on transient errors."""
    import httplib2
    from googleapiclient.errors import HttpError

    response = None
    retry = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status and progress:
                progress(status.progress())
            retry = 0
        except HttpError as exc:
            code = getattr(exc.resp, "status", None)
            if code not in RETRYABLE_STATUS:
                raise UploadError(f"YouTube rejected the upload (HTTP {code}): {_http_reason(exc)}") from None
            error = f"HTTP {code}"
        except (httplib2.HttpLib2Error, ConnectionError, TimeoutError, OSError) as exc:
            error = type(exc).__name__
        else:
            continue
        retry += 1
        if retry > MAX_RETRIES:
            raise UploadError(f"Upload failed after {MAX_RETRIES} retries ({error}).")
        sleep(min(64.0, 2 ** retry) + random.random())
    if "id" not in response:
        raise UploadError("YouTube finished the upload without returning a video ID.")
    return response


def _http_reason(exc) -> str:
    try:
        err = json.loads(exc.content.decode())["error"]
        reasons = ", ".join(e.get("reason", "") for e in err.get("errors", []))
        return f"{err.get('message', '')} [{reasons}]".strip()
    except Exception:
        return "no details"


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    url: str
    privacy: str
    channel_id: str | None
    channel_title: str | None
    warnings: tuple[str, ...]


def upload(plan: UploadPlan, *, interactive: bool, client_secrets: Path = DEFAULT_CLIENT_SECRETS,
           ledger_path: Path = LEDGER_PATH, config: dict | None = None,
           progress: Callable[[float], None] | None = None, service_factory=None,
           take_lock: bool = True, replace_previous: bool = False) -> UploadResult:
    """Upload one video as unlisted, recording it in the ledger. Refuses duplicates.
    Pass take_lock=False only when the caller already holds `upload_lock()`."""
    cfg = config or load_config()
    lock = upload_lock(ledger_path.with_name(LOCK_PATH.name)) if take_lock else nullcontext()
    with lock:
        ledger = Ledger(ledger_path)
        sha = sha256_file(plan.video)
        blocking = ledger.blocking_entry(sha, plan.story_slug, plan.episode)
        if (blocking and replace_previous and blocking["status"] == "done" and blocking.get("sha256") != sha
                and ledger.uploads.get(sha, {}).get("status") not in ("done", "pending")):
            # A NEW file for the same story/episode: record the old video as replaced (the owner removes it
            # from YouTube). The same file is still refused.
            blocking.update(status="replaced", replaced_at=now_utc())
            ledger.save()
            blocking = None
        if blocking:
            raise DuplicateUpload(_describe_block(blocking))

        if service_factory is None:
            from googleapiclient.discovery import build
            creds = load_credentials(interactive=interactive, client_secrets=client_secrets)
            youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        else:
            youtube = service_factory()
        # Refuse BEFORE sending any bytes if this sign-in belongs to another channel.
        check_channel(youtube, cfg.get("expected_channel_id"))
        from googleapiclient.http import MediaFileUpload

        entry = {
            "status": "pending", "sha256": sha, "file": str(plan.video), "bytes": plan.video.stat().st_size,
            "story": plan.story, "story_slug": plan.story_slug, "episode": plan.episode,
            "title": plan.title, "privacy_requested": plan.privacy, "started_at": now_utc(),
        }
        ledger.uploads[sha] = entry
        ledger.save()

        media = MediaFileUpload(str(plan.video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=plan.body(), media_body=media)
        try:
            response = resumable_upload(request, progress=progress)
        except BaseException as exc:
            sent = getattr(request, "resumable_progress", 0) or 0
            if sent >= entry["bytes"]:
                # Every byte reached YouTube but no confirmation came back: the video may exist.
                # Stay "pending" so nothing re-uploads it until a human checks.
                entry.update(error=f"unconfirmed after all bytes were sent: {str(exc)[:250]}")
            else:
                # The upload never completed, so YouTube created no video: safe to retry later.
                entry.update(status="failed", failed_at=now_utc(), error=str(exc)[:300])
            ledger.save()
            raise

        video_id = response["id"]
        privacy = response.get("status", {}).get("privacyStatus", "unknown")
        snippet = response.get("snippet", {})
        warnings: list[str] = []
        if privacy != plan.privacy:
            warnings.append(
                f"YouTube set this video to '{privacy}', not '{plan.privacy}'. Uploads from an API project that has "
                "not passed YouTube's API compliance audit are locked to private.")
        expected = cfg.get("expected_channel_id")
        wrong_channel = bool(expected and snippet.get("channelId") and snippet["channelId"] != expected)
        if wrong_channel:
            warnings.append(f"Uploaded to channel {snippet['channelId']} ({snippet.get('channelTitle')}), "
                            f"not the configured channel {expected}.")
        # A wrong-channel upload does not count as this episode being published.
        entry.update(status="wrong_channel" if wrong_channel else "done", video_id=video_id, uploaded_at=now_utc(), privacy_returned=privacy,
                     channel_id=snippet.get("channelId"), channel_title=snippet.get("channelTitle"),
                     warnings=warnings)
        ledger.save()
        return UploadResult(video_id, f"https://youtu.be/{video_id}", privacy, snippet.get("channelId"),
                            snippet.get("channelTitle"), tuple(warnings))


def _describe_block(entry: dict) -> str:
    where = f"{entry.get('story')} episode {entry.get('episode')}"
    if entry["status"] == "done":
        return f"Already uploaded: {where} -> https://youtu.be/{entry.get('video_id')} ({entry.get('uploaded_at')})."
    return (f"An earlier upload of {where} started at {entry.get('started_at')} but never confirmed. Check YouTube "
            "Studio, then run `youtube_queue.py resolve` to record it as uploaded or not uploaded.")
