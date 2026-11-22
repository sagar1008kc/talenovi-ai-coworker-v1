---
name: release-review
description: Claude's own editorial and technical review of a produced director episode (replaces the human gate) - frames of every shot, identity/lip-sync/motion judgement, targeted reject-shot fixes within caps, editorial_review.md, then approve, 16:9 cut and arc compilation. Never posts.
---
# Release Review

1. **Automated reports.** Read `reports/episode_qa.json`, every shot's `qa` in `shotlist.json` (including advisory `identity` warnings) and `reports/reframe_16x9.json` (after `wide`).
2. **Look at the frames.** View frames from EVERY shot of `master_9x16.mp4`: start, middle and end (`ffmpeg -ss T -frames:v 1`). Check:
   - **Identity:** the same face, age, hair and wardrobe as the bible references; no duplicated people; no extra hands; no stray text or logos.
   - **Performance:** the action reads; the camera actually moves; the background lives; no warping.
   - **Lip sync:** the speaker's mouth moves with the line, and the right person is talking.
   - **Captions:** clear of faces.
   - **Story:** the beat lands, continuity holds across cuts, and the emotional turn is earned.
3. **Fix within caps.** `reject-shot --shot SXX --reason "…" --redo motion|voice|lipsync`, then `produce --confirm-spend` (only that shot's failed parts are regenerated). Respect the retry cap; past it, revise the shot or accept it with a documented note.
4. **Write `reports/editorial_review.md`.** The first line holds the verdict: PASS, FIXED or FAIL. Follow it with the reasons per area (technical, identity, performance and lip sync, story, continuity, originality, PG safety, provenance) and anything that could not be verified (for example, audio not heard by ear).
5. **Release.** `approve --reviewer "claude-code (autonomous)"`, then `wide`, and check that the 16:9 frames don't cut faces. When an arc completes, `compile`.
