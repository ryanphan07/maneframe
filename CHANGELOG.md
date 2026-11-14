# Changelog

## Unreleased

- Launcher: when the model that failed to load is already the smallest in `models.json`, the out-of-memory message no longer tells the user to choose 9; it says: "This PC could not load <model>, the smallest model: not enough graphics memory. Close other programs and start Maneframe again." Other models keep the old text. Internal: `recommend()` lost its unused `gpu_bytes` parameter. No API changes.

## v0.3.1 (tagged 2026-11-13)

- Portable zip: loads the 35B on PCs with an integrated plus a discrete GPU. The launcher no longer passes `-ngl 99` to llama-server, whose automatic fit now splits the model between GPUs and RAM (D20); with `-ngl 99` the 35B ran out of Vulkan memory on a laptop with an Intel iGPU and an 8 GB RTX 4060 (MC-9). Same 41/41 offload and job time within 1.4% on the development PC.
- When llama-server exits while loading, the launcher reads the end of llama-server.log: for an out-of-memory error it says this PC could not load the model and to choose the smaller one with Change model.bat; otherwise it prints the last 5 log lines and the log path (it used to print only the exit code).
- The first-run chooser lists every GPU llama-server reports, one line each. The recommendation rule is unchanged (RAM only, D13). `start.ps1` unchanged. No API changes.

## v0.3.0 (tagged 2026-11-10)

Changes after the v0.2.0 tag. Test counts: see docs/CONVENTIONS.md Commands.

- Portable Windows zip: `scripts/package.ps1` builds `build/maneframe-<version>-win64.zip` (private Python, frontend, backend, llama.cpp b10679 Vulkan). `Start Maneframe.bat` runs `packaging/launcher.py`: free ports (8080 and 8000 preferred), data in `%LOCALAPPDATA%\Maneframe` (`MANEFRAME_DATA_DIR` overrides, launcher only), both servers stopped when the window closes. On first run it recommends the 35B (15 GB of RAM or more) or the 9B (D13; GPU memory is shown only) and downloads the chosen model from Hugging Face with progress, resume and a sha256 check (`packaging/models.json`); `Change model.bat` picks another (DECISIONS D10 to D13). Release preparation: licence texts of Python, llama.cpp and every bundled package in `THIRD_PARTY_NOTICES`; a download that stays under 5 MB/s for 30 s is dropped and resumed (up to 5 times per file, D16); one size unit (binary GB/MB) everywhere and a chooser line with model and vision parts; a longer warm-up wait and a calmer message when it does not finish, announced by a "Warming up the language model" line; the zip contains no ffmpeg (a GPL build): the launcher downloads the pinned imageio-ffmpeg wheel from PyPI at first run (30 MB, sha256-checked, `packaging/ffmpeg.json`, D18) and passes it to the backend as `FFMPEG_PATH`; the README has a "Download and run (Windows)" section covering Unblock, the Windows warning (D15) and where data lives. No API changes; `start.ps1` unchanged.
- Export size options: `aspect=9:16` frames each clip automatically: a crop window that follows the motion, or, for wide action, the whole frame over a blurred copy; captions avoid the performers (no new params; DECISIONS D9); 1080x1920 (source height if smaller); `start` and `end` trim the clip (quips that start inside the range and finish by `end`, shifted). Optional query params on `POST /api/jobs/{id}/export` and `GET .../export.mp4`, defaults unchanged; 400 for bad values. The web app has a size choice and Start/End fields with Set start / Set end buttons next to "Download video".

- Optional offline speech: `TTS_ENGINE=kokoro` (Kokoro via `kokoro-onnx`, CPU, wav, voice or blend in `KOKORO_VOICE`, default af_nicole, `KOKORO_GAIN_DB`; `scripts/voice_samples.py` for auditioning; model and voice files go in the git-ignored `models/kokoro/`; setup in the README).
- Uploads are also converted when the video is H.264 but its long side is above `TRANSCODE_MAX_SIDE` or it is HDR (HLG/PQ), through the same conversion path. Frame rate alone is no trigger. Tested on tiny generated clips only.
- `start.ps1` takes the llama-server host and port from `LLM_BASE_URL`, passes both to llama-server, and stops at once when the port is held by something that is not llama-server (no llama-server `/health` JSON, or `/v1/models` lists no model). Tested against dummy listeners, not a live Docker Desktop.
- A page refresh resumes the job: the job id, filename and style are kept in localStorage; a job the server no longer has shows a notice and clears. Covered by Vitest and a Playwright browser check (`npm run test:e2e`).
- Eval tooling (no effect on the app): `--rotate-terms`/`--seed` in `eval_commentary.py`, `blind_review.py terms`/`humor` options; term counting counts each span of text once, longest match wins ("Twitch chat" is one reference mention).

## v0.2.0 (tagged 2026-11-04)

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
