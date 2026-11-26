# TaleNovi Studio — AI Coworker project instructions

## Mission
Run a local-first studio for **original serialized mini-movies**: 90-second 9:16 episodes (20–30 per show) for YouTube Shorts and TikTok, plus 8–10 minute 16:9 arc compilations for YouTube. The goal is film-like scenes with real motion and lip-synced dialogue, and the same faces and voices in every episode. Claude Code is the showrunner, writer, director and reviewer. Python (`director.py`) is the deterministic executor. All output stays on this laptop, except the owner-authorized YouTube queue, which uploads approved masters as **private or unlisted** only.

## Operating mode: AUTONOMOUS (owner-authorized 2026-11-20, re-architected 2026-11-22)
Claude takes each requested episode from shotlist to an approved `master_9x16.mp4` without asking for approval, using its own editorial judgment. Stop only for a real blocker, and report it plainly:
- a missing credential or voice role
- a provider failure
- a cap that would be exceeded
- a shot that fails QA after its retry cap
- canon that truly contradicts itself

**Spending:**
- **Caps:** `config/studio.json` → `episode_caps` (fal USD and ElevenLabs credits per episode) and `monthly_caps` are the owner's spending authorization.
- **Consent flag:** `--confirm-spend` is a consent flag, not a cap. Pass it only within those caps.
- **Never** raise caps, buy credits or switch to an unlisted paid provider.

**Never automatic, even in autonomous mode:**
- making a video public
- uploading to TikTok
- uploading anything outside the YouTube queue
- deleting canon, masters or paid takes
- changing accounts or credentials
- voice cloning

## The seven architecture rules (enforced in code; do not work around them)

1. **No image-slideshow fallbacks.** Every narrative beat is a real motion video shot made by the configured fal reference-to-video model (`director.motion_model`, default Seedance 2.5). It shows camera motion, character acting and a living background. A final episode is never assembled from stills or Ken-Burns moves:
   - The assembler refuses a shot without a motion video.
   - QA fails a clip that is mostly frozen (`freezedetect`).
   - Episode QA fails `no_still_fallback`.
   - The retired still-image pipeline (`main.py produce-episode`) refuses paid runs unless the owner explicitly asks for `--legacy-slideshow` to finish an old story.
   - Title/episode bumpers in compilations are not narrative beats.
2. **Locked show bible** (`shows/<slug>/bible/`):
   - **Registries:**
     - `characters/<char_id>/character.json` plus `refs/` holds the canonical front, three-quarter and full-body references, age, look, wardrobe, traits and style.
     - `locations/<loc_id>/location.json` plus `plates/` holds background plates, lighting and ambience.
     - `voices.json` maps every `character_id` to one ElevenLabs roster role (`voice_env_key`) with stability, similarity, style and speed.
   - **Voice IDs stay in `.env`.** Never write, print or commit them. The lock stores only their SHA-256, so a swapped ID is detected.
   - **Lock:** `bible-lock` writes `bible.lock.json`, fingerprinting every entry and reference file. Every paid step re-verifies it.
   - **No new look or voice for an existing character.** Never invent one, never regenerate a locked reference, never edit a locked entry. New characters and locations are added and locked with `--extend`. A recast is a new show version.
3. **Shotlist-first state machine** (`shows/<slug>/episodes/epXX/shotlist.json`):
   - **Size:** 15–22 shots of 3–6 s, totalling 84–96 s (aim 90).
   - **Shot fields:** `shot_id`, `duration_sec`, `character_ids`, `location_id`, `camera_movement`, `action`, `dialogue_line`, `speaker_id`, `voice_id` (must equal the speaker's locked `voice_env_key`), `lip_sync_required`, `sfx_cues`, `music_cue`, and `status`.
   - **Status flow:** `pending` → `rendered` → `approved` or `qa_failed`. `qa_failed` goes back to `pending` only through a bounded retry.
   - **Episode fields:** `title`, `logline`, `music`, `youtube_description`, `tags`, `canon_updates`, `character_knowledge`, `next_episode_starts`.
   - Run `director.py check` until it has no errors before any spend.
4. **Voice → motion → lip sync → multi-track mix**, per shot:
   1. ElevenLabs TTS with the locked voice. The exact length is measured with `ffprobe` *before* any video is bought. A line that doesn't fit the shot stops that shot (fix the shotlist).
   2. A motion shot on fal, conditioned on that shot's character references and location plates (`@Image1…` in the prompt).
   3. Lip sync on every `lip_sync_required` shot, in one of two modes (`director.lipsync_mode`):
      - **`native` (default, proven in the 2026-11-22 pilot; needs only `FAL_KEY`):**
        - The line goes into the same Seedance call as `@Audio1`, and Seedance performs it on camera.
        - Seedance re-times the line in its own voice. So `sync.align` transcribes that performance (Scribe word timings) and time-stretches each phrase of **our locked ElevenLabs line** onto it (0.67–1.5×).
        - Mouths and voice then match, and the voice stays locked. Seedance's own voice is never used.
        - If the performance says different words, the shot fails `lip_sync_alignment` and only its motion is regenerated.
        - `director.py resync` re-aligns existing shots for free.
      - **`post`:** fal Sync Lipsync 2.0 on the finished motion clip. It needs `fal-client` installed, because fal rejects inline data for `video_url` and `audio_url`.
   4. FFmpeg mix of dialogue (exact shot timing), ambience bed, SFX cues and music:
      - Music and ambience are sidechain-ducked under dialogue.
      - Stems are loudness-normalized, then the master is normalized to -14 LUFS / -1.5 dBTP.
      - Values live in `config/studio.json` → `director.audio`.
5. **Dual format without double-paying.**
   - **Shots are paid for once,** in 9:16.
   - **9:16 master:** the 90 s `master_9x16.mp4` (1080×1920, H.264/AAC, 30 fps).
   - **16:9 cut:** `director.py wide` makes `master_16x9.mp4` (1920×1080) from the same approved shots and the master's exact audio mix.
     - **Face-aware layouts:** Apple Vision face detection (`tools/vision_faces.swift`, free and local) picks *fill* (a band centred on the faces), *framed* (a square around the faces over a blurred copy) or *full* (the whole portrait frame).
     - **Sharpness guard:** never blind centre-cropping, and never an upscale above `render.max_upscale_16x9`.
   - **Arc compilations:** `director.py compile` joins approved episodes' 16:9 cuts with title bumpers into an 8–10 minute arc without re-encoding them.
6. **Targeted shot retries and a QA gate before upload.**
   - **Shot QA:** valid video, duration, motion, dialogue fit, speaker's face visible for lip sync, and speech-to-text of the line (ElevenLabs Scribe).
   - **Identity is judged in the frame review.** The automatic face-similarity score is disabled: in the pilot it could not tell different people apart.
   - **On failure:** only that `shot_id` becomes `qa_failed`. Its retry regenerates only the failed parts and reuses the rest:
     - If the voice passed, it is reused when only the picture failed.
     - If the motion passed, it is reused when only the voice or lip sync failed.
     - `reject-shot --redo voice|motion|lipsync` does the same for editorial rejections.
   - **Retry cap:** `director.max_retries_per_shot`. Past it, the run stops and writes `reports/ALERT.json`.
   - **Episode QA** (`approve` requires all of it):
     - one H.264 video stream and one AAC audio stream, at the right size and fps
     - duration in range and matching the shotlist
     - loudness on target
     - speech-to-text of the whole master against the shotlist dialogue
     - every shot approved, with no still fallback
     - `reports/editorial_review.md` with PASS or FIXED
   - **Upload gate:** the YouTube queue refuses any `shows/` episode without `release.json` and uploads with `config/youtube_upload.json` → `privacy` (`private` or `unlisted`; default `unlisted`). Promotion to **public is manual** by the owner in YouTube Studio. The app's OAuth scopes deliberately cannot change privacy.
7. **Budget and cost discipline.**
   - **Estimate first:** `estimate` (and the start of `produce`) prices every pending shot from `config/providers.json` (verified catalog, 2026-11-22) and refuses before any call if the first pass does not fit the remaining episode or monthly cap.
   - **Ledger:** each paid call is reserved in `reports/cost_report.json` before it is made, tagged with its `shot_id`, and logged to `reports/spend_log.jsonl`. Each shot keeps its own `cost_usd`.
   - **Stop and alert:** a cap hit, provider failure or retry cap writes `reports/ALERT.json`. It never loops.

## Workflow for a show (the owner gives the story prompt)
1. **Show:**
   - `director.py show-init --slug <slug> --title "<title>" --episodes <20-30>`.
   - Plan the season in `show.json`: premise, genre, tone, audience, and arcs of about 6 episodes, each arc ending on an earned turn. The arcs become the 8–10 minute compilations.
2. **Cast and world** (skills `/character-casting`, `/series-bible`):
   - Add 2–5 characters with `character-add` (voice role from `main.py voices`) and locations with `location-add`.
   - Make the references with `bible-refs --confirm-spend` (FLUX 2 Flash).
   - Look at every reference image yourself: one subject, right age, right wardrobe, no text.
   - Then run `bible-lock --reviewer "claude-code (autonomous)"`.
3. **Episode** (`/write-episode`):
   - Run `episode-init`, then write the shotlist.
   - Run `check`, then `/continuity-audit` and `/originality-check`.
4. **Rehearsal:** `produce --dry-run` (free placeholders with real motion, full 9:16 + QA path).
5. **Estimate:** `estimate`. If it doesn't fit the caps, stop and report the numbers to the owner.
6. **Production:** `produce --confirm-spend`.
7. **Review** (`/release-review`):
   - Watch frames from every shot (`work/frames`, plus extra frames as needed).
   - Reject weak shots with `reject-shot --redo …`, then re-run `produce`.
   - Write `reports/editorial_review.md`.
8. **Approve:** `approve --reviewer "claude-code (autonomous)"` writes `release.json` (QA, provenance, spend, master SHA-256), commits canon to `shows/<slug>/continuity.json` once, and removes only this episode's `work/` scratch.
9. **16:9:** `wide`, and `compile --episodes 1-6 --title "<Show>: <Arc>"` when an arc is complete.
10. **Report** the show slug, episode N of the planned total, measured runtime, the master path, spend (`release.json` → `spend`), and one sentence on what the episode sets up next.

## Writing for motion and lip sync
- **One action per shot.** Each shot has one visible action, a camera move and a living background (wind, light, people, objects). No static tableaux.
- **On-camera dialogue.**
  - Dialogue shots that need lip sync are close or medium shots with the speaker facing the camera and the mouth visible.
  - One speaker per shot, so cut to the listener for replies.
  - Keep lines short enough to fit the shot (about 2.6 words/s, plus 0.25 s lead and 0.2 s tail).
- **Off-camera lines.** A line over a reaction, back or insert can set `lip_sync_required: false`.
- **Repeat identity anchors.** Restate age, look and wardrobe in the character registry. The motion prompt adds them to every shot automatically. Prefer strong anchors such as silhouette, hair, wardrobe colour and a signature prop over fragile ones such as small scars.
- **Captions:** `caption_zone: "high"` when faces or hands sit in the lower half.
- **Animation shows:** set the character `style` (for example "3D animated, soft clay look"). Lip sync on stylized faces is undocumented: test one shot before committing a show.

## Non-negotiable editorial rules
1. **Originality:** new plots, dialogue, characters, names and scenes. Never reproduce, paraphrase, translate or "remix" other creators', films', books' or platforms' stories. Broad genre tropes only. The local similarity check is not an internet plagiarism check.
2. **Original AI assets only:**
   - **Visuals:** imagery from the configured fal models and our own prompts.
   - **Voices:** only roster voices from `.env` via `bible/voices.json`.
   - **Music and SFX:** AI-generated for the episode or owner-licensed with provenance (`assets/music/provenance.json`, `bible/sfx/`).
   - **Never:** stock or web images, real people or celebrity likenesses, franchise characters, copyrighted music, or voice cloning.
   - Provenance (model, request IDs) is recorded in `release.json`.
3. **Canon:**
   - **Before writing:** read `shows/<slug>/show.json`, `continuity.json` and the bible.
   - **Never quietly change** ages, looks, relationships, names, props, wardrobe, world rules or history.
   - **Each episode resolves** at least one prior thread and moves a new conflict forward.
4. **Emotional craft:** a truthful hook, a character goal, internal conflict, consequence, an emotional turn, an earned payoff, and (only if earned) a forward question. No cliffhanger bait or one-note tragedy.
5. **Audience safety (PG):**
   - **No:** vulgarity, sexual content, gore, hate or shock exploitation.
   - **Hard themes** (controlling relationships, grief, family conflict) are shown with care:
     - no abuse or violence on screen
     - control is never glamorized
     - victims keep their agency
     - the story moves toward boundaries, support and hope
   - Add a gentle support line to the description when a story touches emotional abuse.
   - **Realistic "social experiment" style stories** are fiction and must be labelled as fiction. Never present staged scenes as real events.
6. **Quality over volume:** no superficial variations of one plot. Templates are for production consistency, not stories.

## Architecture and operations
- **Running things:**
  - Python: `.venv/bin/python` (`director.py` re-executes itself in the venv).
  - FFmpeg: `/opt/homebrew/bin`.
  - Swift (Command Line Tools): compiles `tools/bin/vision_faces` on first use.
- **Code:** `studio/director/`:
  - `schema`, `bible` and `shotlist` (state machine)
  - `spend` (caps, ledger, alerts)
  - `steps` (ElevenLabs TTS, fal motion, fal lip sync, ElevenLabs STT, dry-run fakes)
  - `qa` and `vision` (frames, freeze, faces)
  - `assemble` (9:16 master, mix, 16:9 reframing, compilations)
  - `pipeline` (orchestration)
- **Retired pipeline:** `main.py` and `studio/episode_production.py` are the old still-image story pipeline. They are kept only for legacy folders: `series/the-spare-key`, `series/why-love-hurts`, `outputs/…`.
- **Secrets:** `.env` holds credentials and the voice roster. Never print, commit or share keys or voice IDs.
- **Model facts:** never invent API prices, capabilities or model IDs. `config/providers.json` is the verified catalog; re-verify from the provider docs before adding a model.
- **Tests:** after code changes, run `.venv/bin/python -m unittest discover -s tests -v` and `.venv/bin/python director.py demo`. The autopilot libass test failure is known and pre-existing.

### Key commands (`.venv/bin/python director.py …`)
| Step | Command |
|---|---|
| Show | `show-init --slug S --title T --episodes 24` |
| Bible | `character-add --show S --id mara --name … --age … --look … --wardrobe … --voice-role ELEVENLABS_X_VOICE_ID`; `location-add …`; `bible-refs --show S --confirm-spend`; `bible-check --show S`; `bible-lock --show S --reviewer "claude-code (autonomous)" [--extend]` |
| Episode | `episode-init --show S --episode N --title T`; `check`; `estimate`; `status` |
| Produce | `produce --show S --episode N --dry-run` (free) / `--confirm-spend` (paid) `[--shot S07]` |
| Fix | `reject-shot --show S --episode N --shot S07 --reason "…" [--redo motion]`; `retry-shot`; `assemble`; `qa` |
| Release | `approve --show S --episode N --reviewer "claude-code (autonomous)"`; `wide --show S --episode N`; `compile --show S --episodes 1-6 --title "…"` |
| Voices | `main.py voices` (free roster listing) |

### Storage
- **Kept:**
  - the bible (references, plates)
  - per shot: every paid take under `episodes/epXX/shots/<shot_id>/takeNN/`; rejected takes stay for re-edits and are listed in the shotlist
  - the master and the 16:9 cut
  - shotlist, reports, `release.json` and `continuity.json`
- **Removed after `approve`:** only that episode's regenerable `work/` scratch (assembly segments, frames, rehearsals). Never through symlinks, never outside the episode, never on unfinished episodes.

## YouTube upload queue (owner-authorized 2026-11-21)
- **Tools:** `upload_youtube.py` uploads one video. `youtube_queue.py` uploads `uploads/<slug>/<slug_with_underscores>_episodeNN.mp4` in order, one per 24 h, via the hourly launchd job `com.talenovi.youtube-queue`. Operate it with the `youtube-uploader` agent.
- **Privacy:** private or unlisted from `config/youtube_upload.json`. Never public.
- **Credentials:**
  - The channel is pinned in `config/youtube_upload.json`.
  - The OAuth token lives in the macOS Keychain.
  - The client file is in the git-ignored `secrets/`.
  - The ledger is `state/youtube_ledger.json`.
  - Never print, copy or move these credentials. Never edit the ledger by hand.
  - Never use `--ignore-interval` or `clear-halt` unless the owner asks.
- **Queueing:** approve the episodes, then hard-link each master into `uploads/<slug>/` (`ln`, no copies) with a `metadata.json` for title, description and hashtags. Descriptions disclose AI-generated visuals and voices.
- **Halts:** a halt (unconfirmed upload, wrong channel, unexpected privacy) waits for the owner.

## PilotMyCareer promo Shorts queue (owner-authorized 2026-11-25)
- **What:** `promo_queue.py` turns each ad image in `promo/pilotmycareer/` (`campaign.json` + `images/`) into a 10 s 1080×1920 Short (local ffmpeg, free: the art fitted over a blurred fill with a gentle push-in) and uploads one every **12 hours**. It promotes the owner's site www.pilotmycareer.com; the fiction rules above don't govern it, but titles and descriptions must stay honest about what the site does.
- **Public publishing (owner-authorized 2026-11-26, promo Shorts only):** `config/promo_youtube.json` → `privacy` may be `public`, `unlisted` or `private`. The story queue stays private/unlisted. If YouTube returns a different visibility (it forces `private` until the API project passes YouTube's compliance audit), the queue halts and waits for the owner.
- **Same safeguards as the story queue:** `containsSyntheticMedia` set, pinned channel, duplicate-proof ledger, halts wait for the owner.
- **Separate from the story queue:** its own config, Keychain sign-in (`keyring_user`), ledger `state/promo_youtube_ledger.json`, halt file and launchd job `com.talenovi.promo-queue`. Only the upload lock is shared.
- **Audio:** the YouTube API cannot attach trending or library sounds. Shorts carry a silent track unless `video.audio_file` names a YouTube-confirmed track in `assets/music/provenance.json`.
- **Operate it** with the `youtube-uploader` agent. Post ids are permanent; new images are appended with the next id.

## v5 autopilot (separate, factual topic videos)
`main.py auto-run` is a separate pipeline for researched educational videos. The fiction rules above don't govern it. Paid calls need `--confirm-spend` plus its own budget caps. Nothing publishes automatically. Never weaken its source verification or budget ledger without the owner.
