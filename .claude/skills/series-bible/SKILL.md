---
name: series-bible
description: Create or extend a director show's world - show.json season plan (20-30 episodes in arcs), locations with plates and ambience, sound/music identity - and keep it consistent with the locked bible.
---
# Show Bible Director

1. **`show.json`.** Premise, genre, tone, PG audience, visual palette, camera style, pacing, music identity, and `season_arcs` (about 6 episodes each: an arc question, the lead's want and choice per episode, the consequence for the next, and the arc payoff). The final arc resolves the central promise.
2. **Locations.** `director.py location-add --show S --id … --name … --description … --lighting … --ambience night|rain|shop|<file in the location folder>`. Plates come from `bible-refs` (no people, no text). Check them, then lock (`bible-lock`, or `--extend` for additions).
3. **Sound.** Store SFX files with provenance under `bible/sfx/`. Use AI music per episode (`music.source: "ai"`), or owner-licensed tracks listed in `assets/music/provenance.json`.
4. **Changing a locked entry** needs a new show version. Additions are fine.
