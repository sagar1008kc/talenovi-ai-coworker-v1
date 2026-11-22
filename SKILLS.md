# TaleNovi Studio — Claude Code skills index

**Actual Claude Code skills** live in `.claude/skills/<skill-name>/SKILL.md`. This file is only a human-readable guide.

| Command in Claude Code | Purpose |
|---|---|
| `/make-episode` | **Autonomous, one episode per request**: "start a new story, X, 5/6 episodes", "continue <slug>", "show status for <slug>" |
| `/character-casting` | Original cast, voice-role assignment from the roster, 5–6-part causal arc, story identity, lock |
| `/series-bible` | Story world, anchors and season plan |
| `/write-episode` | Script + multi-shot storyboard from locked canon |
| `/continuity-audit` | Canon, character and timeline consistency (fixes the draft) |
| `/originality-check` | Duplicate/derivative and asset-rights review |
| `/production-director` | Paid generation and render within `config/studio.json` caps |
| `/release-review` | Claude's frame-by-frame review, bounded fixes, canon approval |

Typical use: *"Start a new story, The Rain Shop, 5 episodes."*, *"Continue the-rain-shop."*, *"Show status for the-rain-shop."* Free helpers: `main.py story-start | story-continue | story-status | review-sheet | reject-take | cleanup`.
Autonomous mode runs without approval stops but never uploads, never raises caps and never buys credits.
