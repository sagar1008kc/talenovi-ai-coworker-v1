---
name: character-casting
description: Design an original recurring cast (2-5 characters) for a 20-30 episode director show, assign each speaking character a fixed ElevenLabs roster role, register them in shows/<slug>/bible, generate and check canonical references, and lock them.
---
# Casting Director (director shows)

1. **Design from the season.** Read `show.json`. Each character serves a dramatic need across the arcs and has a distinct desire, flaw, `deflection` habit, voice and silhouette. Choose the visual style (photoreal cinematic, or a named animation look) once for the show.
2. **Voices.** Run `.venv/bin/python main.py voices` and pick a role per speaking character by gender, age, accent and tone. No two characters share a role. If a needed role is missing, stop and name the exact `ELEVENLABS_<ROLE>_VOICE_ID` for the owner to add.
3. **Register.** `director.py character-add --show S --id <snake_id> --name … --age "…" --look "face, hair, build" --wardrobe "signature outfit and colours" --voice-role ELEVENLABS_X_VOICE_ID --style "…" --trait …`. Anchors must be robust: hair, silhouette, wardrobe colour, a signature prop.
4. **References.** `director.py bible-refs --show S --confirm-spend` (front, three-quarter, full body). View every image: one subject, the stated age, the exact wardrobe, no text or logo. If one is wrong, delete only that unlocked file and re-run.
5. **Lock.** `director.py bible-lock --show S --reviewer "claude-code (autonomous)"` (use `--extend` for later additions). Locked characters never change.
