# TaleNovi Studio AI Coworker — v4 Casting & Continuity Edition

**One master video for YouTube Shorts and TikTok. Original, PG-safe character-driven stories. A locked cast, persistent voices, approved episode canon, and human control over costs and publishing.**

A local-first **Python + Claude Code starter**, not an end-to-end autonomous commercial movie studio. It provides a runnable offline diagnostic and API-connected still-image storytelling pipeline; **motion-video, reference-matched faces, perfectly aligned subtitles, and platform uploads are NOT yet implemented**.

## First 10 minutes: install and test without API charges

Prerequisites: Python 3.11+, `ffmpeg`, `ffprobe`, Git (optional), and Claude Code. Ensure `ffmpeg -version` and `ffprobe -version` work in your terminal.

```bash
# Open the extracted project directory in your terminal.
python -m venv .venv
# macOS / Linux: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python main.py demo
```

The offline demo creates an approximately 12-second, silent diagnostic at `outputs/<episode>/master_9x16.mp4` using **360x640** to test the render quickly. This is **not** a publishable video. Real exports use **1080x1920 H.264/AAC MP4** with no platform watermark. The same master can be uploaded to Shorts and TikTok after human review.

## Step-by-step: create a new original story series

### 1. Initialize a series

```bash
python main.py series-init --slug the-last-signal
```

This creates `series/the-last-signal/series_bible.json` (an **unapproved example draft**) and `continuity.json` (empty approved history). The example cast is illustrative, not locked.

### 2. Have your AI coworker propose characters and season episodes

To use Claude's API to choose a **new original cast based on your concept**, create `.env` from `.env.example` and add a separately billed `ANTHROPIC_API_KEY`. Paid AI casting requires explicit opt-in:

```bash
python main.py cast-propose --series the-last-signal \
  --concept "A radio engineer must choose between saving her brother's memories and her own" \
  --confirm-spend
```

**Alternative without API charges:**

```bash
python main.py cast-template --series the-last-signal
```

This creates an editable draft from the example bible. Either workflow writes `series/the-last-signal/cast_proposal.json`. Review or edit **that proposal**. The AI proposes story-specific characters, including each person's appearance, wardrobe, desire, flaw, relationships, speech style, voice direction and six-episode outline. It does not auto-approve.

### 3. Lock the characters and world after personally reviewing the proposal

```bash
python main.py cast-approve --series the-last-signal --confirm-reviewed
```

This writes the approved series bible and `cast_lock.json` with a SHA-256 fingerprint. **Unapproved edits to the entire bible are detected and blocked.** There is no in-place recasting workflow yet; create a new series or implement a deliberate, versioned migration with human approval. Approved episode history cannot be silently rewritten.

### 4. Select licensed ElevenLabs voices

Create `.env` from `.env.example` (`cp .env.example .env`, or Windows `Copy-Item .env.example .env`). Obtain your own ElevenLabs API key and voice IDs. Use the stable environment-variable names listed in `series_bible.json` under `voices`. For example, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MIRA_VOICE_ID` and `ELEVENLABS_ELIAS_VOICE_ID` for the example cast. **If AI proposes other characters, add their env vars yourself**. Stable settings help voice continuity; they do not guarantee identical acting or intonation.

**Never paste real API keys into GitHub, public prompts, generated stories, or a series bible.**

### 5. Draft one original episode (paid Anthropic API)

```bash
python main.py plan --series the-last-signal \
  --idea "The radio carries a lullaby only Mira and her brother remember" \
  --confirm-spend
```

This creates `outputs/<episode>/story.json`, `script.txt`, and `story_preflight.json`. The writer reads the locked cast and the **approved-only** continuity ledger; drafts cannot change canon. Only **one speaker per scene** is supported in the current speech renderer.

### 6. Inspect the script and approve it before any media charges

```bash
python main.py check --episode "outputs/YOUR-EPISODE-FOLDER"
python main.py approve-script --episode "outputs/YOUR-EPISODE-FOLDER" --confirm-reviewed
```

The script approval records a SHA-256 fingerprint. Editing `story.json` later **invalidates paid-generation approval** until reviewed again. The checker covers speaker IDs, character IDs, episode order, limited PG-language screening, and similarity with approved scripts in your local project; **it does not scan the internet or guarantee worldwide originality**.

### 7. Generate media after approving an actual budget

```bash
python main.py produce --episode "outputs/YOUR-EPISODE-FOLDER" --confirm-spend
```

This calls paid fal.ai **still-image** generation and ElevenLabs narration, then renders one `master_9x16.mp4` and technical QC data. It caches generated media. You must review visual identity, story quality, commercial rights and subtitles manually. `draft_scene_captions.srt` is rough and is NOT ready for publication. The `--confirm-spend` switch is **not** a cost ceiling; monitor provider budgets independently.

### 8. Approve the reviewed video and update approved-only canon

```bash
python main.py approve --episode "outputs/YOUR-EPISODE-FOLDER" --confirm-reviewed
```

Only approve after viewing the full rendered film. This appends the approved episode to `continuity.json`, then you can manually upload the **same single 9:16 MP4** to TikTok and YouTube Shorts. No upload is performed automatically.

## Claude Code prompts and project rules

Launch Claude Code in the extracted folder with `claude`.

Suggested first message:

> Read CLAUDE.md, STORY_STANDARD.md, SKILLS.md, all .claude/rules, and the existing code. Run the offline tests and demo. Use /character-casting to design two or three truly original recurring characters and a complete six-episode emotionally satisfying sci-fi mystery series. Prepare a character reference-image and ElevenLabs voice-audition plan. Show the cast and season before approval. No paid APIs, no publishing, and no unapproved canon changes.

| Important file | Purpose |
|---|---|
| `CLAUDE.md` | Global operating and engineering rules |
| `.claude/rules/*.md` | Always-on safety, originality, casting, continuity and cost rules |
| `.claude/skills/character-casting/SKILL.md` | AI casting director process |
| `.claude/skills/*/SKILL.md` | Episode writer, continuity audit, originality audit, director and release review |
| `SKILLS.md` | Human-friendly skill index (not executable itself) |
| `STORY_STANDARD.md` | Emotional narrative, PG and originality standard |
| `series/<slug>/cast_proposal.json` | Draft cast, **never** approved canon |
| `series/<slug>/series_bible.json` | Approved long-lived character and world definitions |
| `series/<slug>/cast_lock.json` | Cast/world integrity fingerprint |
| `series/<slug>/continuity.json` | Approved-only past events and open threads |
| `outputs/<episode>/` | Local source assets, audio, captions draft, master video, QC and status |

## What works vs. what still needs engineering

| Capability | v4 status |
|---|---|
| Draft cast from an original story using Anthropic API | Implemented, **not live-tested with credentials** |
| Manual cast review and cryptographic integrity lock | Implemented and unit tested |
| Locked character roles and voice profile references | Implemented and unit tested |
| Six-episode emotional story outline proposal | In AI casting prompt, requires human approval and review |
| Series-aware episode generation and canon history | Implemented API adapter; live calls untested |
| Script approval integrity and paid-step gates | Implemented and unit tested |
| Text-anchored fal.ai still-image generation | Implemented API adapter; live calls untested |
| Stable ElevenLabs per-scene speaker configuration | Implemented API adapter; live calls untested |
| Local 9:16 video rendering and FFmpeg QC | Offline demo tested |
| Reference-conditioned consistent character **faces** | **Not implemented** |
| Speaking dialogue with two voices within a single scene | **Not implemented** |
| AI-generated motion clips with Wan/Kling/Veo | **Not implemented** |
| Precise subtitles, burned-in captions and lip sync | **Not implemented** |
| True provider cost ceilings and scheduled multi-agent automation | **Not implemented** |
| Autonomous upload to YouTube / TikTok | **Not implemented; human-only upload** |

## Security and originality

- Save code in a **private GitHub** repo and keep `.env`, voice IDs, local databases and generated media out of Git. Back up character reference images and audio separately.
- Never source plots from TikTok/Reddit/YouTube for close retelling or swap only names. Make new character motivations, world rules, consequences, dialogue and endings.
- The originality checker only flags similarity **within your approved local library**, not against the public internet. Human originality and rights review is mandatory.
- No vulgarity, explicit sexual content, graphic violence, hate, unauthorized likenesses, copyrighted music, or deceptive cliffhanger bait.
- API charges and posting require separate, explicit human authorization; a Claude Code plan/subscription does **not** include third-party API usage.

## Single video settings

Output name: `master_9x16.mp4` • native portrait **9:16** • **1080×1920** full resolution • H.264 video, AAC audio. Measured runtime **60–90 seconds**, aim 75–85 seconds (`config/studio.json`). Demo is 360×640. A TikTok video being over 60 seconds meets only one of Creator Rewards' eligibility conditions; follow the platforms' latest rules.

For optional future motion-provider capabilities and sources, see `PROVIDERS.md`.

---

# v5 Autopilot: researched long-form + Shorts, generated locally on a schedule

A separate, opt-in pipeline for **factual/educational** videos in a configurable niche. The series/fiction
workflow above is unchanged. Nothing is ever uploaded: finished videos wait in a folder for your review.

```
Topic discovery (Claude + web search, duplicate-checked) → Research (sourced claims) → Long-form script
+ standalone Short → AI fact review (+1 auto-revision) → Storyboards → [your script approval]
→ ElevenLabs narration with word timings → FLUX stills → selective image-to-video hero shots
→ thumbnail → FFmpeg edit (16:9 1920x1080 + 9:16 1080x1920) → automated QC → metadata/cost/provenance
```

## Set up (one time)
1. `pip install -r requirements.txt` and install FFmpeg **with libass** (macOS: `brew install ffmpeg`).
2. `cp config/autopilot.example.json config/autopilot.json`, then set `niche`, `audience`, `visual_style`,
   the **budget caps** (per video / per run / per month), and `voice.usd_per_1k_characters` from your
   ElevenLabs plan. Set `voice.commercial_license_confirmed` only after you have checked your plan.
3. In `.env`: `ANTHROPIC_API_KEY`, `FAL_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`
   (optionally `RUNWAYML_API_SECRET`).
4. Optional: add licensed music to `assets/music/` (see its README).
5. `python main.py auto-check` is a no-cost readiness report.

## Run
| Command | What it does | Cost |
|---|---|---|
| `python main.py auto-run --dry-run --preview` | Full rehearsal with offline fakes and small renders | free |
| `python main.py auto-run --confirm-spend` | Resume unfinished videos, then start `videos_per_run` new ones | paid, capped |
| `python main.py auto-status` | Statuses and per-video / month-to-date spend | free |
| `python main.py auto-approve-script --video outputs/autopilot/<folder> --confirm-reviewed` | Approve script + storyboard | free |
| `python main.py schedule-show` | Recommended launchd / Task Scheduler / systemd setup | free |
| `python main.py schedule-install --confirm-unattended-spend` | Writes the scheduler file; prints the load command | enables paid unattended runs |

**Script gate.** With `"script_review": "human"` (the default, matching CLAUDE.md), each video pauses at
`AWAITING_SCRIPT_APPROVAL` after cheap Claude-only stages. Review `script.md`, `storyboard.json` and
`fact_review.json`, approve it, and the next scheduled run produces the media. Approval is bound to a
fingerprint, so editing the files re-closes the gate. `"automated"` skips the human gate; the source
verification and AI fact review still apply. It is your explicit choice and deviates from the
CLAUDE.md rule requiring human script approval before paid media.

## Output (`outputs/autopilot/<date>_<topic-slug>/`, never overwritten)
`<slug>__full_16x9.mp4`, `<slug>__short_9x16.mp4` (burned-in captions), `captions_long.srt` (upload as CC),
`thumbnail.png`, `script.md`, `storyboard.json`, `research.json`, `fact_review.json`, `qc_report.json`,
`metadata.json` (titles, descriptions, tags, sources, AI-disclosure guidance), `cost_report.json`,
`provenance.json`, `assets/` (stills, clips, voice, `jobs.json` with every provider request ID), `work/`.
Run state is kept in `state/` (topic history, monthly spend, lock), and JSON-lines logs in `logs/`.

## Reliability
- Each stage is idempotent and resumable. A paid request ID is saved before polling, so a crash or
  Ctrl-C (or launchd's SIGTERM) never pays twice. Failed stages retry on the next run (3 attempts max).
- Budgets reserve worst-case cost before every paid call. Hitting the run or month cap pauses
  (`PAUSED_BUDGET`); hitting the video cap flags the video for review. Costs are estimates from
  published prices, so reconcile them with provider dashboards.
- Transient HTTP errors back off exponentially. Clip jobs time out (default 20 min), are cancelled, then
  fall back to the next provider, and finally to an animated still.

## QC (automatic, then a human decides)
ffprobe/ffmpeg checks: H.264/yuv420p + AAC, exact resolution, duration window (long 7.6–10.5 min,
Short 30–60 s), A/V length, decode errors, black frames, frozen frames, silence and loudness (−14 LUFS
target). Content checks: caption coverage and order, narration ↔ storyboard coverage, near-duplicate
visuals, every claim backed by a URL Claude actually retrieved, AI fact review, provenance and license
flags, and an AI frame review of sampled frames. A video that fails is marked `NEEDS_REVIEW`; a pass is
marked `COMPLETE_PENDING_HUMAN_REVIEW`. **Passing does not guarantee monetization**: YouTube decides
eligibility, and you must set the "Altered content" disclosure in YouTube Studio when visuals look realistic.

## Status: implemented vs. verified
| Area | Status |
|---|---|
| Provider interface, fal queue, Runway, retries, timeouts, persistence, budgets | Implemented; mocked unit tests pass |
| Claude research/script/storyboard/review calls | Implemented; mocked unit tests pass; **live untested** |
| ElevenLabs timestamps → word-timed captions | Implemented; unit tested offline; **live untested** |
| FFmpeg editing + QC | Implemented; **not yet executed here (FFmpeg not installed on this machine)** |
| Scheduler files (launchd/systemd/schtasks) | Generated and unit tested; not loaded |
| Uploading to YouTube/TikTok | **Not implemented by design** |
