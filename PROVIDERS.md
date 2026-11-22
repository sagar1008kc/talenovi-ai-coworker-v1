# Provider capabilities (verified 2026-11-20)

All model IDs, parameters and prices live in [config/providers.json](config/providers.json), each with its
source URL. Code never hardcodes them. Re-verify before raising budgets; providers change prices often.
"Seconds" below are **per generated clip**, not finished video length.

| Catalog key | Endpoint | Clip length | Resolutions | Price (USD/s) | In code |
|---|---|---|---|---|---|
| `flux2_flash` (stills) | `fal-ai/flux-2/flash` | n/a | custom 512–2048 px | $0.005 / megapixel | Autopilot via fal queue + legacy `main.py produce` |
| `wan3_i2v` / `wan3_t2v` | `alibaba/wan-3.0/{image,text}-to-video` | 2–30 s | 480p/720p/1080p | 0.05 / 0.10 / 0.20 | Implemented, mocked tests, **live untested** |
| `veo31_fast_i2v` | `fal-ai/veo3.1/fast/image-to-video` | 4/6/8 s | 720p/1080p | 0.10 (no audio) | Implemented, mocked tests, **live untested** |
| `kling4_i2v` / `kling4_t2v` | `kling/v4/{image,text}-to-video` | 3–30 s | 720p/1080p | 0.157 / 0.235 | Implemented, mocked tests, **live untested** |
| `seedance25_i2v` / `_t2v` | `bytedance/seedance-2.5/{image,text}-to-video` | 4–30 s | 480p/720p/1080p | 0.2205 / 0.473 / 1.164 | Implemented, mocked tests, **live untested** |
| `flux3max_i2v` / `_t2v` | `blackforestlabs/flux-3-max/{image,text}-to-video` | 5–20 s | 720p (1080p upscaled) | 0.17 / 0.29 (regular) | Implemented, mocked tests, **live untested** |
| `runway_gen45_i2v` (optional) | Runway API `gen4.5` | 2–10 s | 1280:720 / 720:1280 | 0.12 (12 credits) | Implemented, mocked tests, **live untested** |
| ElevenLabs TTS | `/v1/text-to-speech/{voice}/with-timestamps` | full narration | audio | plan-dependent (you set it) | Implemented, mocked tests, **live untested** |
| Claude | `claude-opus-5-5` + `web_search_20260209` | n/a | n/a | $4/$20 per MTok, $10/1k searches | Implemented, mocked tests, **live untested** |

Notes
- **FLUX 3 Max is a video model** on fal (not a still-image model). FLUX 2 Flash remains the still generator.
- Kling 4.0 image-to-video has no `aspect_ratio` input; output follows the start image. Seedance image-to-video is always `auto`.
- fal webhooks are not used: fal drops deliveries to private/loopback addresses, so a laptop polls the queue instead.
- Image inputs for image-to-video are sent as JPEG data URIs (≤ 3.3 MB, Runway's documented limit), or uploaded with
  `fal_client.upload_file` when that optional package is installed. **fal's acceptance of data URIs has not been live-tested.**
- Kling 4.0 had conflicting "API coming soon" notes on some fal pages during verification; the model pages expose a live schema.

Sources: the `source` field of each catalog entry, plus https://fal.ai/docs/model-apis/model-endpoints/queue,
https://docs.dev.runwayml.com/ai-context.md, https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps,
https://support.google.com/youtube/answer/14328491 (AI disclosure).

## Cost-conscious visual strategy (autopilot defaults)
A ~9-minute video is about 65–75 shots. Every shot gets an original FLUX still (~$0.01) animated locally with
Ken Burns moves. Only up to 8 long-form and 3 Short "hero" shots, where motion adds meaning, are animated from
their own still with Wan 3.0 at 720p (~$0.10/s). The fallback order is configurable. If every video provider
fails, the shot degrades to an animated still instead of failing the video.
