#!/usr/bin/env python3
"""TaleNovi motion-first director (series of 90 s episodes, 9:16 masters, 16:9 arc compilations).

Free:  show-init, character-add, location-add, bible-check, bible-lock, episode-init, check, estimate,
       status, produce --dry-run, reject-shot, retry-shot, qa, wide, compile, demo
Paid:  bible-refs --confirm-spend, produce --confirm-spend      (caps: config/studio.json, enforced in code)
Never: uploading or publishing (the YouTube queue reads release.json; public stays manual).

    .venv/bin/python director.py show-init --slug orchard-friends --title "Orchard Friends"
    .venv/bin/python director.py check --show orchard-friends --episode 1
    .venv/bin/python director.py produce --show orchard-friends --episode 1 --dry-run
    .venv/bin/python director.py produce --show orchard-friends --episode 1 --confirm-spend
    .venv/bin/python director.py reject-shot --show orchard-friends --episode 1 --shot S07 --reason "..."
    .venv/bin/python director.py approve --show orchard-friends --episode 1 --reviewer "claude-code (autonomous)"
    .venv/bin/python director.py compile --show orchard-friends --episodes 1-6 --title "Orchard Friends: Arc 1"
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
if sys.prefix == sys.base_prefix and (ROOT / ".venv" / "bin" / "python").exists():
    os.execv(str(ROOT / ".venv" / "bin" / "python"), [str(ROOT / ".venv" / "bin" / "python"), *sys.argv])
os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

from dotenv import dotenv_values  # noqa: E402

from studio.director.bible import BibleError, load_bible  # noqa: E402
from studio.director.pipeline import (Director, DirectorError, compile_arc, episode_dir, load_config,  # noqa: E402
                                      show_dir)
from studio.director.shotlist import (check_shotlist, episode_state, load_shotlist, retry, save_shotlist,  # noqa: E402
                                      write_json)


def env() -> dict:
    return {**os.environ, **{k: v for k, v in dotenv_values(ROOT / ".env").items() if v}}


def out(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def director(a, dry_run: bool = False) -> Director:
    return Director(ROOT, show_dir(ROOT, a.show), episode_dir(ROOT, a.show, a.episode), dry_run=dry_run, env=env())


# ------------------------------------------------------------------ show / bible
def cmd_show_init(a) -> int:
    d = show_dir(ROOT, a.slug)
    if d.exists():
        print(f"{d} already exists; shows are never overwritten.")
        return 1
    for sub in ("bible/characters", "bible/locations", "bible/sfx", "episodes", "compilations"):
        (d / sub).mkdir(parents=True)
    write_json(d / "show.json", {"show_slug": a.slug, "title": a.title, "planned_episodes": a.episodes,
                                 "format": "90 s 9:16 episodes; 16:9 arc compilations of 8-10 min",
                                 "premise": "", "genre": "", "tone": "", "audience": "PG",
                                 "season_arcs": []})
    write_json(d / "bible" / "voices.json", {"_about": "character_id -> locked ElevenLabs roster role (the voice ID "
                                             "stays in .env; the lock stores its SHA-256)"})
    print(f"Created {d}. Next: character-add / location-add, bible-refs, bible-lock.")
    return 0


def cmd_character_add(a) -> int:
    from studio.director.schema import Character, VoiceEntry
    d = show_dir(ROOT, a.show) / "bible" / "characters" / a.id
    bible = load_bible(show_dir(ROOT, a.show))
    if bible.is_locked(a.id):
        print(f"{a.id} is locked; a recast needs a new show version.")
        return 1
    c = Character(character_id=a.id, name=a.name, age=a.age, look=a.look, wardrobe=a.wardrobe,
                  traits=a.trait or [], style=a.style, refs=["front.png", "three_quarter.png", "full_body.png"])
    (d / "refs").mkdir(parents=True, exist_ok=True)
    write_json(d / "character.json", json.loads(c.model_dump_json()))
    vpath = show_dir(ROOT, a.show) / "bible" / "voices.json"
    voices = json.loads(vpath.read_text(encoding="utf-8"))
    voices[a.id] = json.loads(VoiceEntry(voice_env_key=a.voice_role).model_dump_json())
    write_json(vpath, voices)
    print(f"Added {a.id}. Generate refs with bible-refs (paid) or place original front/three_quarter/full_body.png.")
    return 0


def cmd_location_add(a) -> int:
    from studio.director.schema import Location
    d = show_dir(ROOT, a.show) / "bible" / "locations" / a.id
    if load_bible(show_dir(ROOT, a.show)).is_locked(a.id):
        print(f"{a.id} is locked.")
        return 1
    loc = Location(location_id=a.id, name=a.name, description=a.description, lighting=a.lighting,
                   ambience=a.ambience, plates=["plate_a.png", "plate_b.png"])
    (d / "plates").mkdir(parents=True, exist_ok=True)
    write_json(d / "location.json", json.loads(loc.model_dump_json()))
    print(f"Added location {a.id}.")
    return 0


def cmd_bible_refs(a) -> int:
    """Paid (FLUX 2 Flash, ~$0.01 per image): canonical references for unlocked characters/locations."""
    if not a.confirm_spend:
        print("bible-refs makes paid images; pass --confirm-spend (caps apply).")
        return 1
    from studio.director.spend import EpisodeSpend
    from studio.providers import build_image_generator, load_catalog
    from studio.providers.base import JobStore, RetryPolicy
    show = show_dir(ROOT, a.show)
    bible = load_bible(show)
    e = env()
    spend = EpisodeSpend.from_config(ROOT, bible.root, load_config(ROOT))
    gen = build_image_generator(load_catalog(ROOT), "flux2_flash", RetryPolicy(), env=e)
    store = JobStore(bible.root / "reports" / "jobs.json")
    size = {"width": 1088, "height": 1920}
    views = {"front.png": "front view, head and shoulders, looking at camera, neutral expression",
             "three_quarter.png": "three-quarter view, waist up, soft smile",
             "full_body.png": "full body, standing, whole outfit visible, plain background"}
    for cid, c in bible.characters.items():
        if bible.is_locked(cid):
            continue
        for name, view in views.items():
            dest = bible.character_dir(cid) / "refs" / name
            if dest.is_file():
                continue
            prompt = (f"{c.style} character reference, {view}. {c.name}, {c.age}: {c.look}. Wearing {c.wardrobe}. "
                      "One person only, even soft light, clean neutral grey background, no text, no logo.")
            spend.guard_fal(gen.estimate_usd(size), f"ref {cid} {name}")
            gen.generate(key=f"ref:{cid}:{name}", prompt=prompt, size=size, dest=dest, store=store, ledger=spend.ledger)
            print(f"ref {cid}/{name}")
    for lid, loc in bible.locations.items():
        if bible.is_locked(lid):
            continue
        for k, name in enumerate(loc.plates):
            dest = bible.location_dir(lid) / "plates" / name
            if dest.is_file():
                continue
            prompt = (f"Empty establishing background plate, vertical 9:16, {'wide' if k == 0 else 'reverse angle'}: "
                      f"{loc.name}, {loc.description}. Lighting: {loc.lighting}. No people, no hands, no text, no logo.")
            spend.guard_fal(gen.estimate_usd(size), f"plate {lid} {name}")
            gen.generate(key=f"plate:{lid}:{name}", prompt=prompt, size=size, dest=dest, store=store,
                         ledger=spend.ledger)
            print(f"plate {lid}/{name}")
    print("Review every reference image, then run bible-lock.")
    return 0


def cmd_bible_check(a) -> int:
    bible = load_bible(show_dir(ROOT, a.show))
    out({"characters": sorted(bible.characters), "locations": sorted(bible.locations),
         "voices": {k: v.voice_env_key for k, v in bible.voices.items()},
         "errors": bible.validate(), "lock": bible.verify(env()) or "locked and intact"})
    return 0


def cmd_bible_lock(a) -> int:
    bible = load_bible(show_dir(ROOT, a.show))
    try:
        data = bible.lock(env(), a.reviewer, extend=a.extend)
    except BibleError as exc:
        print(f"Not locked: {exc}")
        return 1
    print(f"Locked {len(data['entries'])} entries ({bible.lock_path}).")
    return 0


# ------------------------------------------------------------------ episodes
def cmd_episode_init(a) -> int:
    ep = episode_dir(ROOT, a.show, a.episode)
    if ep.exists():
        print(f"{ep} exists; episodes are never overwritten.")
        return 1
    (ep / "reports").mkdir(parents=True)
    write_json(ep / "shotlist.json", {"show_slug": a.show, "episode_number": a.episode, "title": a.title,
                                      "logline": "", "music": {"source": "ai", "prompt": ""}, "shots": []})
    print(f"Created {ep / 'shotlist.json'} (write 15-22 shots, then: check).")
    return 0


def cmd_check(a) -> int:
    show = show_dir(ROOT, a.show)
    ep = episode_dir(ROOT, a.show, a.episode)
    bible = load_bible(show)
    sl = load_shotlist(ep)
    errors, warnings = check_shotlist(sl, bible, load_config(ROOT)["director"]["episode_format"])
    lock = bible.verify(None)
    out({"runtime_sec": sl.runtime, "shots": len(sl.shots), "errors": errors + [f"bible: {p}" for p in lock],
         "warnings": warnings})
    return 1 if errors or lock else 0


def cmd_estimate(a) -> int:
    out(director(a).estimate())
    return 0


def cmd_status(a) -> int:
    ep = episode_dir(ROOT, a.show, a.episode)
    sl = load_shotlist(ep)
    state = episode_state(sl)
    state["shots_detail"] = [{"shot_id": s.shot_id, "status": s.status, "attempts": s.attempts,
                              "cost_usd": s.cost_usd,
                              "failed": [c["name"] for c in s.qa.get("checks", []) if not c["passed"]]}
                             for s in sl.shots]
    alert = ep / "reports" / "ALERT.json"
    if alert.is_file():
        state["alert"] = json.loads(alert.read_text(encoding="utf-8"))
    state["approved_release"] = (ep / "release.json").is_file()
    out(state)
    return 0


def cmd_produce(a) -> int:
    if a.dry_run == a.confirm_spend:
        print("Choose exactly one: --dry-run (free placeholders) or --confirm-spend (paid, within caps).")
        return 1
    d = director(a, dry_run=a.dry_run)
    try:
        state = d.produce(confirm_spend=a.confirm_spend, only=a.shot or None)
    except Exception as exc:  # report, never loop
        print(f"STOPPED: {type(exc).__name__}: {exc}")
        print(f"See {d.episode / 'reports'} (ALERT.json, estimate.json, shotlist status).")
        return 2
    out(state)
    if a.dry_run:
        print(f"Dry run (placeholders) in {d.episode}")
    return 0


def cmd_reject(a) -> int:
    director(a).reject_shot(a.shot, a.reason, a.redo)
    print(f"{a.shot} -> qa_failed (redo {a.redo or 'voice, motion, lipsync'}). The next produce regenerates only "
          "those parts of this shot.")
    return 0


def cmd_retry(a) -> int:
    ep = episode_dir(ROOT, a.show, a.episode)
    sl = load_shotlist(ep)
    shot = next(s for s in sl.shots if s.shot_id == a.shot)
    retry(shot, load_config(ROOT)["director"]["max_retries_per_shot"], "manual retry")
    save_shotlist(ep, sl)
    print(f"{a.shot} -> pending")
    return 0


def cmd_resync(a) -> int:
    d = director(a)
    out(d.resync(a.shot or None))
    print(d.assemble())
    return 0


def cmd_qa(a) -> int:
    r = director(a).episode_qa()
    out(r)
    return 0 if r["passed"] else 1


def cmd_assemble(a) -> int:
    print(director(a).assemble())
    return 0


def cmd_approve(a) -> int:
    try:
        rel = director(a).approve(a.reviewer)
    except DirectorError as exc:
        print(f"Not approved: {exc}")
        return 1
    print(f"Approved: release.json written (master {rel['master']['seconds']} s, sha256 {rel['master']['sha256'][:12]}...)")
    return 0


def cmd_wide(a) -> int:
    out(director(a).wide())
    return 0


def cmd_compile(a) -> int:
    lo, _, hi = a.episodes.partition("-")
    nums = list(range(int(lo), int(hi or lo) + 1))
    out(compile_arc(ROOT, a.show, nums, a.title))
    return 0


def cmd_demo(a) -> int:
    """Free end-to-end rehearsal in a temp folder: bible, lock, shotlist, dry-run produce, 9:16, 16:9."""
    from tests.director_fixture import build_demo_show
    with tempfile.TemporaryDirectory(prefix="director-demo-") as tmp:
        root = build_demo_show(Path(tmp))
        d = Director(root, root / "shows" / "demo-show", root / "shows" / "demo-show" / "episodes" / "ep01",
                     dry_run=True, env={})
        state = d.produce(confirm_spend=False)
        wide = d.wide()
        qa = json.loads((d.episode / "reports" / "episode_qa.json").read_text())
        out({"state": state, "wide": wide["layouts"], "episode_qa_passed": qa["passed"],
             "failed": [c for c in qa["checks"] if not c["passed"]]})
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, ep=True, show=True):
        sp = sub.add_parser(name)
        if show:
            sp.add_argument("--show", required=True)
        if ep:
            sp.add_argument("--episode", type=int, required=True)
        sp.set_defaults(fn=fn)
        return sp

    s = add("show-init", cmd_show_init, ep=False, show=False)
    s.add_argument("--slug", required=True), s.add_argument("--title", required=True)
    s.add_argument("--episodes", type=int, default=24)
    s = add("character-add", cmd_character_add, ep=False)
    for f in ("--id", "--name", "--age", "--look", "--wardrobe", "--voice-role"):
        s.add_argument(f, required=True)
    s.add_argument("--style", default="photoreal cinematic"), s.add_argument("--trait", action="append")
    s = add("location-add", cmd_location_add, ep=False)
    for f in ("--id", "--name", "--description", "--lighting"):
        s.add_argument(f, required=True)
    s.add_argument("--ambience", default="night")
    s = add("bible-refs", cmd_bible_refs, ep=False)
    s.add_argument("--confirm-spend", action="store_true")
    add("bible-check", cmd_bible_check, ep=False)
    s = add("bible-lock", cmd_bible_lock, ep=False)
    s.add_argument("--reviewer", required=True), s.add_argument("--extend", action="store_true")
    s = add("episode-init", cmd_episode_init)
    s.add_argument("--title", required=True)
    add("check", cmd_check)
    add("estimate", cmd_estimate)
    add("status", cmd_status)
    s = add("produce", cmd_produce)
    s.add_argument("--dry-run", action="store_true"), s.add_argument("--confirm-spend", action="store_true")
    s.add_argument("--shot", action="append", help="limit to these shot_ids (repeatable)")
    s = add("reject-shot", cmd_reject)
    s.add_argument("--shot", required=True), s.add_argument("--reason", required=True)
    s.add_argument("--redo", action="append", choices=["voice", "motion", "lipsync"],
                   help="parts to regenerate (default all); the rest of the take is reused")
    s = add("retry-shot", cmd_retry)
    s.add_argument("--shot", required=True)
    add("assemble", cmd_assemble)
    s = add("resync", cmd_resync)
    s.add_argument("--shot", action="append", help="re-align native lip sync for these shots (free) and re-assemble")
    add("qa", cmd_qa)
    s = add("approve", cmd_approve)
    s.add_argument("--reviewer", required=True)
    add("wide", cmd_wide)
    s = add("compile", cmd_compile, ep=False)
    s.add_argument("--episodes", required=True, help="e.g. 1-6"), s.add_argument("--title", required=True)
    add("demo", cmd_demo, ep=False, show=False)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
