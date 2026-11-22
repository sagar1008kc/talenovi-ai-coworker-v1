---
name: production-director
description: Run the paid director pipeline for a checked shotlist within config/studio.json caps - estimate, voice -> motion -> lip sync per shot with automated QA and bounded targeted retries, then the 9:16 master with a ducked, loudness-normalized multi-track mix.
---
# Production Director

1. **Check and rehearse.** `director.py check`, then `produce --dry-run`; both must pass. The rehearsal tests the whole path for free.
2. **Estimate.** `director.py estimate`. If `fits_caps` is false, stop and report the numbers to the owner. Never lower the resolution or skip lip sync to squeeze under a cap without the owner's word.
3. **Produce.** `director.py produce --show S --episode N --confirm-spend`. For each pending shot:
   1. TTS with the locked voice, measured with ffprobe (a line that doesn't fit stops before any video spend).
   2. fal reference-to-video conditioned on the character references and location plates.
   3. lip sync on `lip_sync_required` shots (native mode: Seedance performs the line as `@Audio1`, then our locked ElevenLabs line is retimed onto that performance; `resync` repeats the alignment for free).
   4. Shot QA (video, duration, motion, face visible for lip sync, speech-to-text).
   - A failing shot retries alone, reusing whatever passed, up to `max_retries_per_shot`.
   - When every shot is approved, it assembles the master and runs episode QA.
4. **When stopped.** Read `reports/ALERT.json` and `status`. Fix the specific shot (`reject-shot`, shotlist revision plus `check`) or report the blocker. Never loop.
5. **Record.** Spend is per shot in `reports/cost_report.json` and `spend_log.jsonl`. Provenance (model and request ID per shot) goes into `release.json` at approve.
