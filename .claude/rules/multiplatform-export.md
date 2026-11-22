# Formats: 9:16 episodes, 16:9 arcs (one paid shot set)

- **Episode master:** `master_9x16.mp4`, 1080×1920, H.264 + AAC 48 kHz, 30 fps, 84–96 s measured (aim 90). One file for YouTube Shorts and TikTok: no watermark, logos or platform overlays.
- **Paid once, in 9:16:** every shot is generated once, portrait, as a real motion video. No still images or Ken-Burns moves stand in for a story beat.
- **16:9 cut:** `master_16x9.mp4`, 1920×1080, reframed from the same approved shots by `director.py wide`.
  - **Face-aware:** fill, framed or full layouts from Apple Vision face boxes; never a blind centre crop.
  - **Sharpness:** never an upscale above `render.max_upscale_16x9`.
  - **Audio:** the 9:16 master's exact mix.
- **Arc compilation:** 8–10 minutes in `shows/<slug>/compilations/`, joined from approved episodes' 16:9 cuts with title bumpers. Never re-generates media.
- **Captions** sit at 66% height, or 30% with `caption_zone: "high"`, in 9:16; in the lower third in 16:9.
- **Finish:**
  - shot sizes vary (wide / medium / close / insert)
  - each shot has a real camera move and on-screen motion
  - transitions: hard cuts within scenes; `fadeblack` for time jumps
  - dialogue lip-synced on camera
  - music and ambience ducked under dialogue; master at -14 LUFS / -1.5 dBTP
- **Uploading:** stays manual except the owner-authorized YouTube queue (private or unlisted, `containsSyntheticMedia` set). TikTok and making videos public stay manual.
