# Licensed background music (optional)

The autopilot never downloads music. Put tracks you have commercial rights to here and list
them in `provenance.json`. Only tracks with `"commercial_use_confirmed": true` are used.

```json
{"tracks": [
  {"file": "calm-piano.mp3", "title": "Calm Piano", "license": "Purchased license #1234, YouTube monetization allowed",
   "commercial_use_confirmed": true, "source_url": "https://...", "mood": "calm"}
]}
```
Without tracks, videos render narration-only and QC adds a warning.
