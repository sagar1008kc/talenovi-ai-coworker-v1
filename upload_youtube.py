#!/usr/bin/env python3
"""Upload one video to YouTube as UNLISTED.

    .venv/bin/python upload_youtube.py --video "<video.mp4>" --story "<story name>" --episode 3 \
        --title "<episode title>" --description "<episode summary>" --hashtags "ShortStory,Mystery,Episode3"

Title:       "{story} | Episode {episode} | {title}"
Description: summary, a blank line, then the hashtags on one line (exactly one # each).
Visibility:  always status.privacyStatus = "unlisted".

--dry-run prints exactly what would be sent, without signing in or uploading.
First real run opens your browser to authorize the channel; the token is kept
in the macOS Keychain, never in the project, logs or terminal.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_VENV_PY = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
try:  # `python upload_youtube.py` with a system Python: switch to the project venv
    import googleapiclient  # noqa: F401
except ImportError:
    if _VENV_PY.exists() and Path(sys.prefix).resolve() != _VENV_PY.parent.parent.resolve():
        os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise

from studio.youtube_upload import (DEFAULT_CLIENT_SECRETS, LEDGER_PATH, PRIVACY, DuplicateUpload, Ledger,
                                   UploadError, make_plan, sha256_file, upload)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Upload one video to YouTube as unlisted.")
    p.add_argument("--video", required=True, help="Path to the .mp4 file")
    p.add_argument("--story", required=True, help="Story name, e.g. 'The Spare Key'")
    p.add_argument("--episode", type=int, help="Episode number (1, 2, ...); not used with --full-story")
    p.add_argument("--title", default="", help="Episode title (optional subtitle with --full-story)")
    p.add_argument("--full-story", action="store_true",
                   help='Whole-story compilation: title "{story} | Full Story[ | {title}]"')
    p.add_argument("--description", required=True, help="Episode summary")
    p.add_argument("--hashtags", default="", help="Comma-separated, with or without '#'")
    p.add_argument("--replace-previous", action="store_true",
                   help="Upload a NEW file for an already-uploaded story/episode; the old video is marked replaced")
    p.add_argument("--story-slug", help="Ledger key for the story (default: derived from --story)")
    p.add_argument("--dry-run", action="store_true", help="Preview only: no sign-in, no upload")
    p.add_argument("--non-interactive", action="store_true", help="Never open a browser; fail if not authorized")
    p.add_argument("--client-secrets", type=Path, default=DEFAULT_CLIENT_SECRETS, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if not args.full_story and (args.episode is None or not args.title):
        p.error("--episode and --title are required (unless --full-story)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = make_plan(args.video, args.story, args.episode or 0, args.title, args.description, args.hashtags,
                         story_slug=args.story_slug, full_story=args.full_story)
        if args.dry_run:
            print("DRY RUN: nothing will be signed in to or uploaded.\n")
            print(plan.preview())
            blocking = Ledger(LEDGER_PATH).blocking_entry(sha256_file(plan.video), plan.story_slug, plan.episode)
            if blocking:
                print(f"\nNOTE: a real run would be refused: {blocking.get('story')} episode "
                      f"{blocking.get('episode')} is already {blocking['status']} in the upload ledger.")
            return 0

        def progress(fraction: float) -> None:
            print(f"\r  uploading... {fraction * 100:5.1f}%", end="", flush=True)

        print(plan.preview(), "\n")
        result = upload(plan, interactive=not args.non_interactive, client_secrets=args.client_secrets,
                        progress=progress, replace_previous=args.replace_previous)
        print("\r" + " " * 30 + "\r", end="")
        for w in result.warnings:
            print(f"WARNING: {w}", file=sys.stderr)
        if result.privacy == PRIVACY:
            print(f"Uploaded as UNLISTED: {result.url}")
        else:
            print(f"Uploaded, but YouTube reports visibility '{result.privacy}': {result.url}")
        if result.channel_title:
            print(f"Channel: {result.channel_title} ({result.channel_id})")
        return 0 if not result.warnings else 3
    except DuplicateUpload as exc:
        print(f"Not uploaded (duplicate protection): {exc}", file=sys.stderr)
        return 4
    except UploadError as exc:
        print(f"\nUpload failed: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled before YouTube confirmed the upload (see state/youtube_ledger.json).", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
