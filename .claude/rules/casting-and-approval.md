# Casting, character identity and approval (always applies)

- **Origin:** characters come from original, story-driven needs, not demographics, trends or existing IP. Cast 2–5 recurring characters per show, each with a distinct desire, flaw, voice and visual silhouette.
- **Voices:**
  - Pick each speaking character's ElevenLabs roster role from `main.py voices` (gender, age, accent, tone) and register it with `character-add --voice-role ELEVENLABS_<ROLE>_VOICE_ID`.
  - Two characters never share a role.
  - Never invent voice IDs or write them into the repo.
  - If no suitable role exists, stop and name the exact variable the owner should add.
  - Never clone voices.
- **References:**
  - Canonical references (front, three-quarter, full body) and location plates come from `bible-refs` (FLUX 2 Flash), or from original owner art.
  - Claude checks every image (one subject, right age, wardrobe, no text or logo) before `bible-lock --reviewer "claude-code (autonomous)"`.
  - Prefer robust anchors (silhouette, hair, wardrobe colour, a signature prop) over fragile ones (small scars, jewellery).
- **Fiction only:** characters are wholly fictional and AI-generated. No real-person, celebrity or franchise likeness. Minors appear only as synthetic characters in PG situations.
- **After the lock** (`bible.lock.json`, SHA-256 of every entry and file plus a hash of each resolved voice ID), identity is immutable. Every paid step verifies the lock.
- **Separate approvals:** the bible lock, shot approvals (automated QA plus editorial `reject-shot`) and episode approval (`approve`, with `editorial_review.md`) are separate recorded actions. Nothing publishes by itself.
