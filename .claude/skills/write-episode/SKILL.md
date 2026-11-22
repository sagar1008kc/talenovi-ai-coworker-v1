---
name: write-episode
description: Write one original 90-second TaleNovi episode as shows/<slug>/episodes/epXX/shotlist.json (15-22 motion shots of 3-6 s with on-camera lip-synced dialogue) from the locked bible and continuity, ready for director.py produce.
---
# Episode Writer (shotlist-first)

**Read first:** `show.json` (season arcs), `continuity.json` (canon, character knowledge, `next_episode_starts`), and the bible (characters, locations, `voices.json`). Use only registered `character_id`s and `location_id`s. If a new one is needed, add it with `character-add` or `location-add`, give it references, and lock it with `bible-lock --extend` first.

**Story.** Write the episode as a short film:
- Open on an observable action that gives a reason to care. In later episodes, a changed object, decision or relationship orients the returning viewer.
- Give the lead a goal, an internal conflict, a choice with a consequence, an emotional turn, and an earned payoff. End on a forward question only if it's earned.
- Resolve at least one prior thread and move one new conflict forward.

**Shotlist** (`shotlist.json`; schema in `studio/director/schema.py`):

Episode level:
- **Size:** 15–22 shots, 3–6 s each, totalling 84–96 s (aim 90).
- **Fields:** `title`, `logline`, `music` (`{"source": "ai", "prompt": …}`, `library` or `none`), `youtube_description`, `tags`, `canon_updates`, `character_knowledge`, `next_episode_starts`.

Each shot:
- **Picture:** `shot_id` (S01…), `duration_sec`, `character_ids` (only those visible), `location_id`, `camera_movement` (a real move, e.g. `push_in`, `tracking`, `orbit_left`), `framing`, `action`.
- **`action`:** one concrete visible beat with acting and a living background. Never a static tableau.
- **Dialogue:** `dialogue_line`, `speaker_id`, `voice_id` (exactly the speaker's `voice_env_key` from `bible/voices.json`), `lip_sync_required`.
- **Lip-sync shots:** one speaker, on camera, close or medium, face visible. Replies cut to the other speaker. Lines over reactions or backs set `lip_sync_required: false`.
- **Fit:** dialogue fits the shot (about 2.6 words/s plus 0.45 s padding). Draft every line aloud in that character's voice and give it an intention; cut lines the picture already shows.
- **Sound and edit:** `sfx_cues` (files in `bible/sfx/` with provenance), `music_cue`, `transition_in` (`cut` by default; `fadeblack` for time jumps), `caption_zone` (`high` when faces or hands sit low).

**Continuity across cuts:** eyelines, wardrobe, prop ownership, time of day, and what each character knows.

**Finish:** run `director.py check --show S --episode N` until errors are zero, and deal with each warning.
