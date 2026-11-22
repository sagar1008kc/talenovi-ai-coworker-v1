# Engineering, costs and data safety (always applies)

- **Caps:** paid spend is authorized only within `config/studio.json` caps (`episode_caps`: fal USD and ElevenLabs credits per episode; `monthly_caps`).
  - `--confirm-spend` is a consent flag, not a cap. The caps are enforced in code before every paid call.
  - Never raise caps, buy credits or switch to an unlisted paid provider without the owner.
- **Estimate first:** `director.py estimate` (also the first step of `produce`) refuses the whole run if the first pass doesn't fit. Report the numbers to the owner instead of trimming quality silently.
- **Retries:** bounded per shot (`director.max_retries_per_shot`), and only the failed parts are regenerated (voice, motion or lip sync). Passing parts are reused. Never re-run a whole episode to fix one shot. A cap hit, provider failure or exhausted retries writes `reports/ALERT.json` and stops.
- **Ledger:** every paid call is reserved before it is made (`reports/cost_report.json` with `shot_id`, plus `reports/spend_log.jsonl`). Paid outputs are cached per take and never bought twice. 16:9 cuts and compilations reuse approved shots for free.
- **Never automatic:** publishing, deleting masters, paid takes or canon, account or credential changes, purchases, voice cloning. The only exception is `approve`, which removes that episode's own regenerable `work/` scratch: never through symlinks, never outside the episode, never protected files.
- **Secrets:** never print credentials or voice IDs. Keep `.env` free of anything committed.
- **Validate before paying:** `check`, then `produce --dry-run`, before paid generation. Run the dry run again after any change to the shotlist schema or the renderer.
- **Model facts:** `config/providers.json` is the verified catalog (model IDs, parameters, prices, sources, date). Never invent a model, parameter or price. Re-verify from the provider docs before adding one.
- **Code style:** explicit errors, safe local paths, subprocess argument lists, immutable historical decisions, unit tests (`tests/test_director.py`).
- **Two kinds of QA:** automated QA checks technical properties (streams, duration, motion, faces, speech-to-text, loudness). Claude's editorial review (frames, story, performance, identity) is separate and mandatory, recorded in `reports/editorial_review.md`.
