# Changelog

## v0.2.0 (unreleased; not tagged)

Changes since v0.1.0.

- Renamed the project from LionHype to Maneframe.
- Schema-constrained model output: the request carries a JSON schema (`LLM_JSON_SCHEMA`, default on); a server that rejects it (HTTP 400) is retried once without it, and a reply with extra JSON objects is still read.
- Window skipping: a window the model fails on is retried once, then skipped and reported with its time range; the job fails if the server is unreachable, over half the windows are skipped, or 3 in a row.
- mp4 export: "Download video" renders H.264 + AAC with the original audio ducked, quips mixed in and optional burned-in captions.
- Export loudness: each quip is compressed and normalized to -14 LUFS and the original is ducked 14 dB (`EXPORT_QUIP_LUFS`, `EXPORT_DUCK_DB`); commentary now sits 11 to 19 dB above the ducked original for both TTS engines.
- Phone clip conversion is capped at 1080p and 30 fps (`TRANSCODE_MAX_SIDE`, `TRANSCODE_MAX_FPS`) and tone-maps HDR to SDR: a 4K 120 fps HDR iPhone clip took 25+ minutes before, about 1 minute now.
- `start.ps1` warms the model with one tiny request.
- Commentary styles (`hype`, `deadpan`, `broadcast`) chosen in a dropdown, served by `GET /api/styles`.
- Scene continuity: each window gets the last quips and what the model saw before, plus a lion dance glossary in the system prompt.
- Optional offline speech: `TTS_ENGINE=piper` (Piper, CPU, wav) with `PIPER_MODEL_PATH` and `PIPER_LENGTH_SCALE`; edge-tts stays the default. Setup in the README.
- Eval script (`backend/scripts/eval_commentary.py`): LLM-only runs to CSV with a rating summary.
- Moment labels: add, list, delete and export (CSV) labels on the player timeline, stored by video content hash.
- Phone-clip transcode: HEVC, undecodable and rotated uploads are converted to upright H.264 as the job's first step, with progress; the prepared video is served with Range support.
- Label options come from the top-level glossary terms (indentation-aware parser), so the owner's nested steps and section headers are not offered as labels.
- Eval scripts write and read CSVs as UTF-8 with a BOM, so Excel shows curly quotes correctly; blind review gained a notable flag, a named-move failure type and an owner humor sheet.
- The owner's glossary and four example lines are in the system prompt (about 2.5K more prompt tokens, about 25% slower per window; grounding unchanged, see docs/research/grounding.md).
- Progressive results: audio is synthesized per window and the finished commentary is shown and playable while the job still runs.
- Faster frame sampling: one seek per window, then a sequential read; the crop-box probe is a single sequential pass.
- One-command start: `start.ps1` checks the setup, starts llama-server if nothing answers, serves the built frontend from the backend and opens the app; it stops after 30 s if the model port is taken but `/health` does not answer.
- Frontend tests (Vitest + Testing Library): playback fallback, progressive refresh, style dropdown.
- A quip shift or drop is logged once per job instead of on every later window.
- Unusable frame rates are rejected with a clear message; `benchmark.py` records the job id and takes `--style`.
