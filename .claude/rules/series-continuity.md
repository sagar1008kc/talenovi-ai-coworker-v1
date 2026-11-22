# Show continuity (always applies)

- **Read first:** before writing an episode or touching a character, read `shows/<slug>/show.json`, `continuity.json` and the bible (`bible/characters`, `bible/locations`, `bible/voices.json`). Work only within that slug.
- **Stable IDs:** `show_slug`, `episode_number`, `character_id`, `location_id`, `speaker_id`, and `voice_id` (= the speaker's locked `voice_env_key`). Never use a loose variant.
- **Locked anchors** (in `bible.lock.json`): reference images, look, age, wardrobe, style, voice role and settings, locations and plates. Never regenerate or edit a locked entry. New characters and locations are added and locked with `--extend`. A recast is a new show version.
- **Canon:** each shotlist records `canon_updates`, `character_knowledge` and `next_episode_starts`. `approve` commits them to `continuity.json` exactly once, after episode QA and an editorial PASS. Never on drafts, rehearsals or failed renders.
- **No retcons** without story evidence and a documented `canon_updates` entry.
- **Threads:** each episode resolves at least one prior thread while moving a new conflict forward, and each arc of about 6 episodes ends on an earned turn.
- **Ambiguity:** if continuity is ambiguous, resolve it in the story's favour using canon and note it in `reports/editorial_review.md`. Ask the owner only if canon truly contradicts itself.
- **Legacy:** `series/<slug>/` stories (the old pipeline) keep their own `series_bible.json`, `cast_lock.json` and `continuity.json`.
