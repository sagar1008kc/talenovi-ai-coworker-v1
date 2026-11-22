#!/usr/bin/env python3
"""Widescreen deliverable for an approved story episode (free, no paid calls).

    .venv/bin/python make_exports.py --story why-love-hurts --episode 01

Writes next to the approved 1080x1920 master, in the episode folder:
    master_16x9.mp4    1920x1080  free 16:9 re-render from the episode's retained sources
                                  (studio/widescreen.py), with the master's exact audio mix
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

from studio.widescreen import render_episode  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--story", required=True)
    p.add_argument("--episode", required=True, help="Episode folder name, e.g. 01")
    args = p.parse_args(argv)
    ep = ROOT / "series" / args.story / "episodes" / args.episode
    if not (ep / "release.json").is_file():
        print(f"{ep} is not approved yet (no release.json); exports are made only from approved masters.")
        return 1
    out = ep / "master_16x9.mp4"
    partial = ep / ".master_16x9.partial.mp4"
    with tempfile.TemporaryDirectory(prefix="wide-") as tmp:
        render_episode(ROOT, ep, partial, Path(tmp) / "work")
    partial.replace(out)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
