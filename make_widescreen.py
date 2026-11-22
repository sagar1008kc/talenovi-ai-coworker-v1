#!/usr/bin/env python3
"""Free 16:9 full-story video from an approved 9:16 story (no paid calls).

    .venv/bin/python make_widescreen.py --story the-spare-key
        -> uploads/fullstory/the_spare_key_full_story.mp4 (1920x1080, all approved episodes in order)

    .venv/bin/python make_widescreen.py --story the-spare-key --episode 01 --out /tmp/ep01.mp4
        -> one episode only (for checking the crop)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

from studio.widescreen import render_episode, render_story  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--story", required=True, help="Story slug under series/, e.g. the-spare-key")
    p.add_argument("--episode", help="Render only this episode folder name, e.g. 01")
    p.add_argument("--out", type=Path, help="Output MP4 path")
    args = p.parse_args(argv)
    if args.episode:
        ep = ROOT / "series" / args.story / "episodes" / args.episode
        out = args.out or ROOT / "uploads" / "fullstory" / f"{args.story.replace('-', '_')}_episode{args.episode}_16x9.mp4"
        with tempfile.TemporaryDirectory(prefix="widescreen-") as tmp:
            render_episode(ROOT, ep, out, Path(tmp))
    else:
        out = args.out or ROOT / "uploads" / "fullstory" / f"{args.story.replace('-', '_')}_full_story.mp4"
        render_story(ROOT, args.story, out)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
