---
name: continuity-audit
description: Audit a director episode shotlist against show canon, the locked bible (looks, wardrobe, voices, locations), chronology and character knowledge; fix the draft autonomously before any spend.
---
# Continuity Audit

1. **Read** `shows/<slug>/show.json`, `continuity.json`, the bible (characters, locations, `voices.json`) and the draft `episodes/epXX/shotlist.json`. (Legacy `series/<slug>` stories use `main.py check` and `story.json` instead.)
2. **Run** `.venv/bin/python director.py check --show S --episode N` (free). Zero errors are required.
3. **Characters, per shot:**
   - registered `character_ids` only
   - the speaker is on camera for lip-sync shots
   - `voice_id` equals the speaker's locked role
   - the action matches the character's age, look, wardrobe and props
   - relationships and motivation follow canon
4. **Story logic:** chronology, places, world rules and unresolved threads. Apparent contradictions need a causal reason inside the episode.
5. **Canon fields:**
   - Fill `canon_updates`, `character_knowledge` (what each character knows now) and `next_episode_starts`.
   - No character acts on knowledge they don't have yet.
   - Eyelines, wardrobe, prop ownership and time of day hold across consecutive shots.
6. **Fix and log.** Fix defects yourself before production. Log PASS / FIXED / FAIL with shot references in `reports/editorial_review.md`. Escalate to the owner only for genuinely contradictory canon.
