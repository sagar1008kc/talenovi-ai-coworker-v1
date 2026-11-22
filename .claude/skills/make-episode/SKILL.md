---
name: make-episode
description: Autonomously make ONE finished 90-second TaleNovi episode per request with director.py - "start a new show <title/concept>, <20-30> episodes", "next episode of <slug>" / "continue <slug>", or "status of <slug>" - bible, shotlist, real motion shots with lip sync, QA, editorial review, approve, 16:9 cut, and an arc compilation when an arc completes.
---
# Make Episode (director pipeline, one episode per request)

Follow CLAUDE.md. Use `.venv/bin/python director.py`. Never use `main.py produce-episode` for new work.

**New show** ("start a new show …"):
1. Run `show-init`.
2. Plan the season in `show.json`: premise, genre, tone, PG audience, 20–30 episodes in arcs of about 6, each arc ending on an earned turn.
3. Run `/character-casting` and `/series-bible` to fill the bible, then `bible-refs --confirm-spend`.
4. Check every reference image, then run `bible-lock`.
5. Continue with episode 1 below.

**Episode** (new show, or "continue"):
1. **Find the next episode.** Read `continuity.json` (`latest_episode`, `next_episode_starts`). An episode folder without `release.json` is unfinished: resume it, don't start a new one.
2. **Write.** Run `episode-init` and `/write-episode`, then `check` until it has no errors. Then run `/continuity-audit` and `/originality-check`.
3. **Rehearse.** `produce --dry-run`, then read `work/dryrun/reports/episode_qa.json`.
4. **Estimate.** If `fits_caps` is false, STOP. Report the estimate and caps to the owner; never cut quality silently.
5. **Produce.** `produce --confirm-spend`. On `STOPPED`, read `reports/ALERT.json`:
   - **retry_limit:** revise that shot (staging, action, length) and re-run.
   - **budget or provider:** report to the owner.
6. **Review.** Run `/release-review`: frames of every shot, then `reject-shot --redo …` for weak ones, re-run `produce`, and write `reports/editorial_review.md`.
7. **Release.**
   - `approve --reviewer "claude-code (autonomous)"`, then `wide`.
   - If this completes an arc, run `compile --episodes <first>-<last> --title "<Show>: <Arc name>"` and check that it is 8–10 min.
8. **Report** the show slug, episode N of the total, measured runtime, master and 16:9 paths, spend (`release.json` → `spend`), and one sentence on what this episode sets up next.

**Status:** run `status` for each episode folder and read `continuity.json`. No media calls.

Uploading happens only if the owner asks to queue the show (hard-link masters into `uploads/<slug>/`, see CLAUDE.md).
