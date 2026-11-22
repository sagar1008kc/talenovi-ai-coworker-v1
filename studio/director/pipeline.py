"""Director orchestration: estimate -> per-shot (voice -> motion -> lip sync -> QA) -> assemble -> episode QA
-> release. Deterministic and resumable: shot state lives in shotlist.json, paid outputs are cached per
take, and a failed shot is retried on its own (bounded) without touching any other shot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import shutil
from typing import Callable

from studio.director import assemble, qa, vision
from studio.director.bible import Bible, load_bible, sha256_file
from studio.director.schema import Shot, Shotlist
from studio.director.shotlist import (RetryLimitReached, check_shotlist, episode_state, load_shotlist, retry,
                                      save_shotlist, transition, write_json)
from studio.director.spend import EpisodeSpend, SpendCapExceeded, raise_alert
from studio.director.steps import Steps, ff, text_sha
from studio.models import utc_now
from studio.providers import load_catalog
from studio.providers.base import ProviderError

EP_RE = re.compile(r"^ep(\d{2})$")


class DirectorError(RuntimeError):
    pass


def load_config(root: Path) -> dict:
    return json.loads((root / "config" / "studio.json").read_text(encoding="utf-8"))


def show_dir(root: Path, slug: str) -> Path:
    return root / load_config(root)["director"]["shows_dir"] / slug


def episode_dir(root: Path, slug: str, number: int) -> Path:
    return show_dir(root, slug) / "episodes" / f"ep{number:02d}"


@dataclass
class Director:
    root: Path
    show: Path                    # shows/<slug>
    episode: Path                 # shows/<slug>/episodes/epXX
    dry_run: bool = False
    env: dict = field(default_factory=lambda: dict(os.environ))
    log: Callable[[str], None] = print
    http: object = None

    def __post_init__(self) -> None:
        self.config = load_config(self.root)
        self.cfg = self.config["director"]
        self.catalog = load_catalog(self.root)
        self.bible: Bible = load_bible(self.show)
        if self.dry_run:
            self._rehearsal()
        self.sl: Shotlist = load_shotlist(self.episode)
        self.spend = EpisodeSpend.from_config(self.root, self.episode, self.config)
        if self.dry_run:          # rehearsals never touch the real ledger or monthly total
            self.spend.ledger.month.path = self.episode / "reports" / "dryrun_month.json"
        self.steps = Steps(self.root, self.episode, self.bible, self.catalog, self.cfg, self.spend,
                           dry_run=self.dry_run, env=self.env, log=self.log, http=self.http)

    def _rehearsal(self) -> None:
        """Dry runs work on a copy in <episode>/work/dryrun so placeholders never mix with paid takes."""
        target = self.episode / "work" / "dryrun"
        if target.is_symlink():
            raise DirectorError(f"refusing symlinked rehearsal folder {target}")
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        sl = load_shotlist(self.episode)
        for s in sl.shots:
            s.status, s.attempts, s.assets, s.qa, s.cost_usd, s.history = "pending", 0, {}, {}, 0.0, []
        save_shotlist(target, sl)
        self.episode = target

    # ------------------------------------------------------------ gates
    def preflight(self) -> dict:
        fmt = self.cfg["episode_format"]
        errors, warnings = check_shotlist(self.sl, self.bible, fmt)
        problems = self.bible.verify(None if self.dry_run else self.env)
        errors += [f"bible: {p}" for p in problems]
        if not self.dry_run:
            for k in ("FAL_KEY", "ELEVENLABS_API_KEY"):
                if not self.env.get(k):
                    errors.append(f"{k} is not set in .env")
        if not self.dry_run and any(s.lip_sync_required and not self.steps.native_lipsync(s) for s in self.sl.shots):
            try:
                import fal_client  # noqa: F401
            except ImportError:
                errors.append("lip sync needs fal's official uploader: .venv/bin/pip install -r requirements.txt "
                              "(fal-client). fal rejects inline data for video_url/audio_url.")
        for b in ("ffmpeg", "ffprobe"):
            if shutil.which(b) is None:
                errors.append(f"{b} not on PATH")
        result = {"passed": not errors, "errors": errors, "warnings": warnings, "vision": vision.available()}
        write_json(self.episode / "reports" / "preflight.json", result)
        return result

    def estimate(self) -> dict:
        lip = self.catalog["lipsync_models"][self.cfg["lipsync_model"]]
        stt = self.catalog["stt_models"][self.cfg["stt_model"]]
        res = self.cfg["motion_resolution"]
        todo = [s for s in self.sl.shots if s.status in ("pending", "qa_failed")]

        def billed(s: Shot) -> tuple[int, float]:
            spec = self.catalog["video_models"][s.motion_model or self.cfg["motion_model"]]
            allowed = spec["duration"]["values"]
            secs = next((v for v in allowed if v >= math.ceil(s.duration_sec)), allowed[-1])
            interrupted = s.attempts > 0 and len(s.assets.get("rejected", [])) < s.attempts
            cached = self.episode / "shots" / s.shot_id / f"take{s.attempts:02d}" / "motion.mp4"
            if s.status == "pending" and interrupted and cached.is_file():
                return 0, 0.0                       # paid already; the interrupted take resumes from cache
            return secs, secs * spec["usd_per_second"][res]
        per = [billed(s) for s in todo]
        motion_s = sum(p[0] for p in per)
        spec = self.catalog["video_models"][self.cfg["motion_model"]]
        lip_s = sum(s.duration_sec for s in todo if s.lip_sync_required and not self.steps.native_lipsync(s))
        chars = sum(len(s.dialogue_line) for s in todo)
        motion_usd = sum(p[1] for p in per)
        lip_usd = lip_s * lip["usd_per_second"]
        stt_usd = (len(todo) * 10 + self.sl.runtime) / 3600 * stt["usd_per_hour"]
        fal = motion_usd + lip_usd
        retries = self.cfg["max_retries_per_shot"]
        est = {"shots_to_render": len(todo), "motion_model": spec["model_id"], "resolution": res,
               "per_shot": {s.shot_id: {"model": s.motion_model or self.cfg["motion_model"], "billed_s": p[0],
                                        "usd": round(p[1], 3)} for s, p in zip(todo, per)},
               "motion_seconds": motion_s, "motion_usd": round(motion_usd, 2), "lipsync_seconds": round(lip_s, 2),
               "lipsync_usd": round(lip_usd, 2), "voice_characters": chars, "stt_usd": round(stt_usd, 3),
               "fal_usd_first_pass": round(fal, 2),
               "fal_usd_if_every_shot_retried_once": round(fal * 2, 2),
               "worst_case_fal_usd": round(fal * (1 + retries), 2),
               "caps": {"fal_per_episode": self.spend.max_fal_usd, "credits_per_episode": self.spend.max_voice_credits,
                        "monthly_usd": self.spend.monthly_cap_usd, "month_spent_usd": round(self.spend.month_spent(), 2)},
               "fal_spent_this_episode": round(self.spend.fal_spent(), 2)}
        remaining = min(self.spend.max_fal_usd - self.spend.fal_spent(),
                        self.spend.monthly_cap_usd - self.spend.month_spent())
        est["fits_caps"] = self.dry_run or (fal <= remaining + 1e-9 and chars <= self.spend.max_voice_credits
                                            - self.spend.credits_spent())
        write_json(self.episode / "reports" / "estimate.json", est)
        return est

    # ------------------------------------------------------------ per shot
    def render_shot(self, shot: Shot) -> None:
        # An interrupted take (crash/provider stop before QA) resumes in place: its paid outputs are cached.
        interrupted = shot.attempts > 0 and len(shot.assets.get("rejected", [])) < shot.attempts
        if not interrupted:
            shot.attempts += 1
        take = self.steps.shot_dir(shot)
        prev = (shot.assets.get("rejected") or [None])[-1]
        redo = set(prev.get("redo", [])) if prev else {"voice", "motion", "lipsync"}
        cur: dict = {"take": str(take.relative_to(self.episode)), "started_utc": utc_now(), "reused": []}
        pv = (prev or {}).get("voice")
        if pv and "voice" not in redo and pv.get("text_sha256") == text_sha(shot.dialogue_line) \
                and (self.episode / pv["file"]).is_file():
            voice = pv                                    # same line, passed STT: reuse, never re-buy
            cur["reused"].append("voice")
        else:
            voice = self.steps.voice(shot)
        fmt = self.cfg["episode_format"]
        if voice:
            cur["voice"] = voice
            fits = voice["seconds"] + fmt["dialogue_lead_seconds"] + fmt["dialogue_tail_seconds"]
            if fits > shot.duration_sec + 1e-6:
                # Measured BEFORE any video spend: no motion is bought for a line that cannot fit.
                shot.assets["current"] = cur
                shot.qa = {"passed": False, "checks": [qa.check("dialogue_fits", False,
                           f"line is {voice['seconds']:.2f} s; shot is {shot.duration_sec} s (lengthen the shot "
                           "up to the max, split the line, or trim words)")]}
                transition(shot, "qa_failed", "dialogue longer than the shot (no video bought)")
                return
            cur["dialogue_wav"] = str(self.steps.padded_dialogue(shot, self.episode / voice["file"])
                                      .relative_to(self.episode))
        pm = (prev or {}).get("motion")
        if pm and "motion" not in redo and (self.episode / pm["file"]).is_file():
            motion = pm                                   # picture passed QA: only voice/lip sync are redone
            cur["reused"].append("motion")
        else:
            motion = self.steps.motion(shot, self.episode / cur["dialogue_wav"] if cur.get("dialogue_wav") else None)
        cur["motion"] = motion
        video = self.episode / motion["file"]
        if motion.get("native_lipsync"):
            cur["sync"] = self.sync_to_performance(shot, take, video, self.episode / voice["file"])
            if cur["sync"]["ok"]:
                cur["dialogue_wav"] = cur["sync"]["file"]
        if shot.lip_sync_required and not motion.get("native_lipsync"):
            lip = self.steps.lipsync(shot, video, self.episode / cur["dialogue_wav"], motion.get("remote_url"))
            cur["lipsync"] = lip
            video = self.episode / lip["file"]
        cur["video"] = str(video.relative_to(self.episode))
        shot.assets["current"] = cur
        shot.cost_usd = round(self.spend.by_shot().get(shot.shot_id, 0.0), 4)
        transition(shot, "rendered", f"take {shot.attempts}")

    def sync_to_performance(self, shot: Shot, take: Path, video: Path, tts: Path) -> dict:
        """Retime our locked-voice line onto the motion model's own spoken performance (free locally;
        one speech-to-text call). The result file replaces the padded dialogue for this shot."""
        from studio.director import sync
        alignment = sync.load_alignment(tts)
        perf = take / "performance.wav"
        if self.dry_run:
            words = self.steps.transcribe_words(perf, shot.dialogue_line, "", alignment)[1]
        else:
            ff("-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(perf))
            words = self.steps.transcribe_words(perf, shot.dialogue_line, f"{shot.shot_id} performance stt")[1]
        dest = take / "dialogue_synced.wav"
        report = sync.align(tts, alignment, words, shot.duration_sec, dest) if words else \
            {"ok": False, "reason": "no speech found in the performance"}
        report["file"] = str(dest.relative_to(self.episode))
        return report

    def resync(self, only: list[str] | None = None) -> list[dict]:
        """Free re-alignment of already generated native-lip-sync shots (no new video), then re-assemble."""
        out = []
        for shot in self.sl.shots:
            cur = shot.assets.get("current", {})
            if (only and shot.shot_id not in only) or not cur.get("motion", {}).get("native_lipsync"):
                continue
            take = self.episode / cur["take"]
            cur["sync"] = self.sync_to_performance(shot, take, self.episode / cur["motion"]["file"],
                                                   self.episode / cur["voice"]["file"])
            if cur["sync"]["ok"]:
                cur["dialogue_wav"] = cur["sync"]["file"]
            chk = qa.check("lip_sync_alignment", cur["sync"]["ok"],
                           cur["sync"].get("reason") or f"worst phrase tempo {cur['sync'].get('worst_tempo')}")
            shot.qa["checks"] = [c for c in shot.qa.get("checks", []) if c["name"] != "lip_sync_alignment"] + [chk]
            if not chk["passed"] and shot.status == "approved":
                shot.qa["passed"] = False
                transition(shot, "qa_failed", "lip sync alignment failed on resync")
            out.append({"shot_id": shot.shot_id, **{k: cur["sync"].get(k) for k in ("ok", "worst_tempo", "reason")}})
        save_shotlist(self.episode, self.sl)
        return out

    def qa_shot(self, shot: Shot) -> bool:
        cur = shot.assets["current"]
        video = self.episode / cur["video"]
        frames = self.episode / "work" / "frames" / shot.shot_id
        heard = None
        if cur.get("voice"):
            heard = self.steps.transcribe(self.episode / cur["voice"]["file"], shot.dialogue_line,
                                          f"{shot.shot_id} stt")
        checks = qa.shot_checks(shot, video, cur.get("voice"), self.cfg, frames, heard, self.dry_run)
        if "sync" in cur:
            checks.append(qa.check("lip_sync_alignment", cur["sync"]["ok"],
                                   cur["sync"].get("reason") or f"worst phrase tempo {cur['sync'].get('worst_tempo')}"))
        if not self.dry_run and self.cfg["qa"].get("identity_distance_warn"):
            checks += qa.identity_notes(shot, video, self.bible, frames, self.cfg["qa"]["identity_distance_warn"])
        ok = qa.passed(checks)
        shot.qa = {"passed": ok, "checks": checks, "utc": utc_now()}
        transition(shot, "approved" if ok else "qa_failed",
                   "automated QA passed" if ok else "; ".join(c["name"] for c in checks if not c["passed"]))
        return ok

    # ------------------------------------------------------------ episode run
    def produce(self, confirm_spend: bool, only: list[str] | None = None) -> dict:
        if not confirm_spend and not self.dry_run:
            raise DirectorError("paid generation needs --confirm-spend (a consent flag; caps still apply)")
        pre = self.preflight()
        if not pre["passed"]:
            raise DirectorError(f"preflight failed: {pre['errors']}")
        est = self.estimate()
        self.log(f"estimate: ${est['fal_usd_first_pass']} fal first pass for {est['shots_to_render']} shots "
                 f"(cap ${est['caps']['fal_per_episode']}, month ${est['caps']['month_spent_usd']} of "
                 f"${est['caps']['monthly_usd']})")
        if not est["fits_caps"]:
            raise_alert(self.episode, "budget", "first-pass estimate exceeds the remaining caps", estimate=est)
            raise SpendCapExceeded(f"Estimate ${est['fal_usd_first_pass']} does not fit the remaining caps "
                                   f"{est['caps']}. Nothing was generated. Only the owner can change the caps.")
        max_r = self.cfg["max_retries_per_shot"]
        try:
            for shot in self.sl.shots:
                if only and shot.shot_id not in only:
                    continue
                while shot.status != "approved":
                    if shot.status == "qa_failed":
                        retry(shot, max_r, "automatic retry after QA failure")
                    if shot.status == "pending":
                        self.render_shot(shot)
                        save_shotlist(self.episode, self.sl)
                    if shot.status == "rendered":
                        self.qa_shot(shot)
                        save_shotlist(self.episode, self.sl)
                    if shot.status == "qa_failed" and shot.qa.get("checks") and \
                            any(c["name"] == "dialogue_fits" and not c["passed"] for c in shot.qa["checks"]):
                        raise RetryLimitReached(f"{shot.shot_id}: the line does not fit the shot; revise the "
                                                "shotlist (no retry can fix this)")
                    self.log(f"{shot.shot_id}: {shot.status} (take {shot.attempts})")
        except RetryLimitReached as exc:
            save_shotlist(self.episode, self.sl)
            raise_alert(self.episode, "retry_limit", str(exc), state=episode_state(self.sl))
            raise
        except (SpendCapExceeded, ProviderError) as exc:
            save_shotlist(self.episode, self.sl)
            raise_alert(self.episode, "budget" if isinstance(exc, SpendCapExceeded) else "provider", str(exc),
                        spend=self.spend.summary(), state=episode_state(self.sl))
            raise
        save_shotlist(self.episode, self.sl)
        alert = self.episode / "reports" / "ALERT.json"
        if alert.is_file():                       # keep the record, mark it resolved by this successful run
            alert.replace(alert.with_name(f"ALERT.resolved-{utc_now().replace(':', '')[:15]}.json"))
        state = episode_state(self.sl)
        write_json(self.episode / "reports" / "spend_summary.json", self.spend.summary())
        if state["all_approved"]:
            state["master"] = self.assemble()
        return state

    # ------------------------------------------------------------ assembly / QA / release
    def music_file(self, work: Path) -> Path | None:
        m = self.sl.music or {}
        src = m.get("source", "none")
        if src == "none":
            return None
        if src == "library":
            from studio.music import library_track
            path, _ = library_track(self.root, m["file"])
            return path
        dest = self.episode / "audio" / "score.mp3"
        if self.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            ff("-f", "lavfi", "-i", f"sine=frequency=164:duration={self.sl.runtime:.2f}", "-af", "volume=0.2",
               "-c:a", "libmp3lame", str(dest))
            return dest
        from studio.music import MUSIC_MODEL, compose_ai_score, music_credits
        compose_ai_score(api_key=self.env["ELEVENLABS_API_KEY"], prompt=m["prompt"], seconds=self.sl.runtime,
                         dest=dest, ledger=self.spend.ledger,
                         guard=lambda credits, item: self.spend.guard_credits(credits, item))
        self.spend.note("episode", "music", 0.0, credits=music_credits(self.sl.runtime), model=MUSIC_MODEL)
        return dest

    def assemble(self) -> str:
        not_ok = [s.shot_id for s in self.sl.shots if s.status != "approved"]
        if not_ok:
            raise DirectorError(f"cannot assemble: shots not approved {not_ok}")
        work = self.episode / "work" / "assemble"
        music = self.music_file(work)
        info = assemble.episode_master(self.root, self.show, self.episode, self.sl, self.bible, self.cfg, music,
                                       work, self.episode / "master_9x16.mp4")
        write_json(self.episode / "reports" / "assembly.json", info)
        self.episode_qa()
        return info["master"]

    def episode_qa(self) -> dict:
        master = self.episode / "master_9x16.mp4"
        fmt = self.cfg["episode_format"]
        heard = None
        if any(s.dialogue_line for s in self.sl.shots):
            if self.dry_run:
                heard = " ".join(s.dialogue_line for s in self.sl.shots)
            else:
                audio = self.episode / "work" / "assemble" / "master_audio.wav"
                audio.parent.mkdir(parents=True, exist_ok=True)
                ff("-i", str(master), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio))
                heard = self.steps.transcribe(audio, "", "episode stt")
        r = self.cfg["render"]
        lo, hi = ((self.sl.runtime - 1, self.sl.runtime + 1) if self.sl.pilot
                  else (fmt["min_seconds"], fmt["max_seconds"]))
        checks = qa.episode_checks(master, self.sl, self.cfg, heard, r["width"], r["height"], lo, hi)
        result = {"passed": qa.passed(checks), "checks": checks, "utc": utc_now(), "dry_run": self.dry_run}
        write_json(self.episode / "reports" / "episode_qa.json", result)
        return result

    def reject_shot(self, shot_id: str, reason: str, redo: list[str] | None = None) -> None:
        shot = next((s for s in self.sl.shots if s.shot_id == shot_id), None)
        if shot is None:
            raise DirectorError(f"no shot {shot_id}")
        if shot.status not in ("approved", "rendered"):
            raise DirectorError(f"{shot_id} is {shot.status}; only rendered/approved shots are rejected")
        shot.qa.setdefault("checks", []).append(qa.check("editorial_review", False, reason))
        shot.qa["passed"] = False
        bad = set(redo or ["voice", "motion", "lipsync"]) - {"voice", "motion", "lipsync"}
        if bad:
            raise DirectorError(f"--redo accepts voice, motion, lipsync (got {sorted(bad)})")
        shot.qa["redo"] = sorted(set(redo or ["voice", "motion", "lipsync"]) |
                                 ({"lipsync"} if shot.lip_sync_required else set()))
        transition(shot, "qa_failed", f"editorial review: {reason} (redo {shot.qa['redo']})")
        save_shotlist(self.episode, self.sl)

    def approve(self, reviewer: str) -> dict:
        """Release record (upload gate): episode QA passed, editorial review PASS, master checksum."""
        if self.dry_run or self.sl.pilot:
            raise DirectorError("dry runs and pilots are never approved (nothing to upload or commit)")
        result = self.episode_qa()
        if not result["passed"]:
            raise DirectorError(f"episode QA failed: {[c for c in result['checks'] if not c['passed']]}")
        review = self.episode / "reports" / "editorial_review.md"
        if not review.is_file() or not re.search(r"\b(PASS|FIXED)\b", review.read_text(encoding="utf-8")[:400]):
            raise DirectorError("reports/editorial_review.md with a PASS or FIXED verdict is required (Claude's "
                                "frame + story review)")
        if not (self.sl.canon_updates and self.sl.next_episode_starts and self.sl.character_knowledge):
            raise DirectorError("shotlist needs canon_updates, character_knowledge and next_episode_starts")
        master = self.episode / "master_9x16.mp4"
        release = {"show_slug": self.sl.show_slug, "episode_number": self.sl.episode_number,
                   "title": f"{self.sl.title} | {self.sl.show_slug} ep{self.sl.episode_number:02d}",
                   "approved_by": reviewer, "approved_utc": utc_now(),
                   "qc": {"passed": True, "checks": result["checks"]},
                   "master": {"file": "master_9x16.mp4", "sha256": sha256_file(master),
                              "bytes": master.stat().st_size, "seconds": round(vision.duration(master), 3)},
                   "spend": self.spend.summary(),
                   "provenance": [{"shot_id": s.shot_id, "motion": s.assets["current"].get("motion", {}).get("model"),
                                   "motion_request": s.assets["current"].get("motion", {}).get("request_id"),
                                   "lipsync": s.assets["current"].get("lipsync", {}).get("model"),
                                   "voice": (s.assets["current"].get("voice") or {}).get("voice_env_key")}
                                  for s in self.sl.shots],
                   "privacy_default": self.cfg["upload"]["default_privacy"]}
        write_json(self.episode / "release.json", release)
        self.commit_canon()
        release["storage"] = self.cleanup()
        write_json(self.episode / "release.json", release)
        return release

    def cleanup(self) -> dict:
        """After approve only: delete regenerable scratch inside THIS episode's work/ (segments, frames,
        rehearsals). Paid takes (shots/), the master, shotlist, reports and canon are never touched."""
        if not (self.episode / "release.json").is_file():
            raise DirectorError("cleanup runs only after approve")
        freed, removed = 0, []
        work = (self.episode / "work").resolve()
        for name in ("assemble", "frames", "dryrun", "wide"):
            target = self.episode / "work" / name
            if not target.exists() or target.is_symlink() or not target.resolve().is_relative_to(work):
                continue
            for f in target.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    freed += f.stat().st_size
            shutil.rmtree(target)
            removed.append(str(target.relative_to(self.episode)))
        return {"removed": removed, "freed_bytes": freed}

    def commit_canon(self) -> None:
        """Append this episode to shows/<slug>/continuity.json exactly once (after QA + editorial PASS)."""
        path = self.show / "continuity.json"
        canon = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"episodes": {}}
        key = f"{self.sl.episode_number:02d}"
        if key in canon["episodes"]:
            return
        canon["episodes"][key] = {"title": self.sl.title, "logline": self.sl.logline,
                                  "canon_updates": self.sl.canon_updates,
                                  "character_knowledge": self.sl.character_knowledge,
                                  "next_episode_starts": self.sl.next_episode_starts, "committed_utc": utc_now()}
        canon["latest_episode"] = max(int(k) for k in canon["episodes"])
        write_json(path, canon)

    def wide(self) -> dict:
        if not (self.episode / "release.json").is_file() and not (self.dry_run or self.sl.pilot):
            raise DirectorError("16:9 cuts are made only from approved episodes (no release.json)")
        return assemble.episode_wide(self.episode, self.sl, self.cfg, self.episode / "master_9x16.mp4",
                                     self.episode / "work" / "wide", self.episode / "master_16x9.mp4")


def compile_arc(root: Path, slug: str, numbers: list[int], title: str, log: Callable[[str], None] = print) -> dict:
    """8-10 minute 16:9 arc from approved episodes' 16:9 cuts (rendered free from their paid shots if missing)."""
    cfg = load_config(root)["director"]
    parts = []
    for n in numbers:
        ep = episode_dir(root, slug, n)
        if not (ep / "release.json").is_file():
            raise DirectorError(f"episode {n:02d} is not approved")
        wide = ep / "master_16x9.mp4"
        if not wide.is_file():
            log(f"rendering 16:9 cut of ep{n:02d} from its approved shots ...")
            Director(root, show_dir(root, slug), ep).wide()
        sl = load_shotlist(ep)
        parts.append((f"Episode {n} | {sl.title}", wide))
    out = show_dir(root, slug) / "compilations" / f"{slug.replace('-', '_')}_arc_{numbers[0]:02d}-{numbers[-1]:02d}.mp4"
    info = assemble.compilation(parts, title, cfg, out.parent / "work", out)
    lo, hi = cfg["compilation"]["min_minutes"] * 60, cfg["compilation"]["max_minutes"] * 60
    info["length_check"] = "pass" if lo <= info["seconds"] <= hi else f"outside {lo}-{hi} s"
    write_json(out.with_suffix(".json"), info)
    return info
