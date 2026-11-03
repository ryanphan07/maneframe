# Maneframe: AI lion dance commentator

Upload a lion dance performance clip and get humorous, time-synced play-by-play commentary, read aloud while the video plays. A local vision LLM watches the footage, edge-tts (or, offline, Piper) speaks the lines, and the server schedules them so they never overlap.

Status: v0.1.0 is tagged; changes since are in [`CHANGELOG.md`](CHANGELOG.md) (v0.2.0, not yet tagged). a working local prototype that is being developed into a tool for lion dance teams who turn performance clips into shareable posts (roadmap: `docs/TASKS.md`). It runs end to end with no fake output: if the model or TTS fails, you see an error or a "no audio" notice, never placeholder content.

![demo](docs/demo.gif)

[Full clip with sound](docs/demo.mp4) (screen recording of the app playing a 40 s clip with generated commentary).

## How it works

```
Browser (React/Vite)                    FastAPI backend                         Local services
  upload ---------------------------->  POST /api/upload  (check name and size, save, create job)
  progress <---- WS /ws/analyze/{id} -  background pipeline
                                          1 extracting    OpenCV: duration, motion crop box (one sequential pass)
                                          2 analyzing     10 s windows, 6 frames each ------> llama-server
                                                          (image_url parts, JSON quips) <---- (multimodal Qwen, --mmproj)
                                          3 synthesizing  edge-tts per quip --------------> Microsoft edge-tts (internet)
                                                          mp3 durations (mutagen), schedule so audio never overlaps
  result <---- GET /api/jobs/{id} ----  quips: timestamp, text, audio_url, duration_sec
  plays video + queued audio clips in sync (video ducks while a clip plays)
```

Key design points:
- Motion-based crop: wide stage shots are cropped once per video to where the bright, moving performers are, so the model sees them at useful resolution.
- Windowed analysis: one LLM call per 10 s window, passing the last two quips to avoid repeats.
- Server-side scheduling: an audio line never starts before the previous one ends plus 0.3 s. It is shifted up to 2 s later, otherwise dropped. Lines without audio are still shown.
- Styles and continuity: `hype`, `deadpan` or `broadcast` commentary from a dropdown; each window gets the previous quips and a lion dance glossary. Output is schema-constrained (`LLM_JSON_SCHEMA`).
- Progressive results: audio is synthesized per window, so the finished part of the commentary plays while the rest is still being analyzed.
- Phone clips: HEVC, unreadable and rotated uploads are converted to upright H.264 first (at most 1080p and 30 fps, HDR tone-mapped to SDR; a 40 s 4K 120 fps HDR clip converts in about a minute).
- mp4 export, moment labels (CSV) and an eval script (`backend/scripts/eval_commentary.py`) for judging commentary quality.
- One command: `start.ps1` starts the model and the app.
- Honest failure: a window the model fails on (after one retry) is skipped and reported ("skipped n of m parts" with the time ranges), never filled with invented text; if every window fails the job errors. A failed TTS clip only removes that line's audio.

## Requirements
- Windows (developed natively on Windows 11), Python 3.12, Node 20+ (tested on 24).
- A running llama-server with a multimodal Qwen model and its mmproj file.
- Internet access for edge-tts (an unofficial Microsoft endpoint; needs `edge-tts>=7.3.1`), or the offline Piper voice below.

## Quickstart (PowerShell, from the repo root)

One command, after the one-time setup:

```powershell
# One-time setup
cd backend; python -m venv venv; venv\Scripts\Activate.ps1; pip install -r requirements.txt; cd ..
copy backend\.env.example backend\.env   # then set LLAMA_SERVER_PATH, LLM_MODEL_PATH and LLM_MMPROJ_PATH (and LLM_CTX if needed)

.\start.ps1
```
`start.ps1` checks the venv, ffmpeg and the frontend build (it builds the frontend if `frontend\dist` is missing), starts llama-server only if `/health` does not answer (log in `logs\`), waits for it, starts the backend on port 8000 (which also serves the built frontend) and opens http://localhost:8000. Ctrl+C stops only the processes the script started; a llama-server that was already running is left alone.

Manual path (run the pieces yourself; `npm run dev` gives hot reload on port 5173):

```powershell
# 1. LLM (separate terminal; adjust paths)
llama-server -m <model.gguf> --mmproj <mmproj-F16.gguf> -c 32768 -np 1 -ngl 99 --port 8080 --jinja

# 2. Backend
cd backend; venv\Scripts\Activate.ps1
uvicorn app.main:app --port 8000          # also serves frontend\dist at http://localhost:8000 when it is built

# 3. Frontend dev server (another terminal; optional)
cd frontend; npm install; npm run dev     # http://localhost:5173
```
Open the app, choose a clip (`.mp4`, `.mov`, `.avi`; at most 30 min and 2 GB by default, see `MAX_VIDEO_SECONDS`/`MAX_UPLOAD_MB`), wait for the progress bar, press play. When it finishes, "Download video" renders a shareable mp4 (H.264 + AAC, original size, optional captions). Expect minutes of processing: on Qwen3.5-9B a 40 s clip took about 4 minutes (see Benchmarks). Short clips are the supported demo; 40 s is the longest clip measured.

## Offline voice (Piper, optional)

edge-tts needs internet. `TTS_ENGINE=piper` speaks with [Piper](https://github.com/OHF-Voice/piper1-gpl), which runs on the CPU with no network (about 0.05 s per line after a 2 s first load, see `docs/BENCHMARKS.md`). The default stays `edge`. Piper writes wav files; the player and the mp4 export take them like the mp3s.

```powershell
backendenv\Scripts\Activate.ps1
pip install piper-tts==1.8.0
python -m piper.download_voices en_US-lessac-medium --data-dir models/piper
```

Then put `TTS_ENGINE=piper` in `backend/.env` and restart. The voice (two files, about 63 MB) goes in `models/piper/`, which is git-ignored; never commit voice models. Other voices: `PIPER_MODEL_PATH=models/piper/<name>.onnx` (relative paths start at the repo root; list voices with `python -m piper.download_voices`; check each voice's licence). `PIPER_LENGTH_SCALE` below 1 speaks faster. If Piper or the voice file is missing, or a line fails to synthesize, that line has no audio and the log says why; nothing is substituted.


| Route | Purpose |
|---|---|
| `GET /api/styles` | `{styles: [...], default}`: the files in `prompts/styles/` (the values `style` accepts) |
| `POST /api/upload` (multipart `file`, optional form field `style`) | Checks the name, extension and size, saves the clip and starts a job; returns `{job_id, filename}` without reading the video. `style` must be one of `GET /api/styles`, else 400. 400 `{"detail": ...}` on bad input. Conversion to upright H.264 (when needed), the corruption check and the length limit are the job's first step (status `extracting`, message `Converting video`); a clip that fails them ends the job in `error` with the reason |
| `GET /api/jobs/{job_id}` | `{job_id, status, progress, message, result}`; `result` is `{quips: [{timestamp, text, audio_url, duration_sec}], audio_available, style, ...}` once completed; while running `{partial: {quips, windows_done, total_windows, ready_until}}` or null (finished quips so far, never shown as complete). 404 if unknown |
| `WS /ws/analyze/{job_id}` | Server to client only: `{status, progress, message?}` with status in `uploading, extracting, analyzing, synthesizing, completed, error`. Sends the current state first, closes after a terminal state; unknown job closes with 4404 |
| `POST /api/jobs/{job_id}/export?captions=1` | Renders the mp4 (same rules and errors as the GET below, errors as JSON) and returns `{"url": "/api/jobs/{id}/export.mp4?captions=1"}`; the web app calls this first, then downloads from the URL so the file is never held in browser memory |
| `GET /api/jobs/{job_id}/export.mp4?captions=1` | Renders the finished job with ffmpeg and returns an mp4: original audio ducked 14 dB while a quip plays, each quip compressed and normalized to -14 LUFS (both TTS engines come out equally loud), quips mixed in at their timestamps, quip text burned in at the bottom when `captions` is 1 (default). Cached per job and captions flag. 409 until completed, 404 if the source video or audio is gone, 500 `{"detail": ...}` if ffmpeg fails |
| `GET /api/jobs/{job_id}/labels` | `{labels: [{id, t, label, quality, note, created_at}], label_options, qualities}`; labels are sorted by time. `label_options` are the terms in `prompts/lion_dance_glossary.txt` plus `other`; `qualities` are `good`, `needs work`, `none`. 404 if the job or its video is gone |
| `POST /api/jobs/{job_id}/labels` | Body `{t, label, quality, note?}`; returns the new label with 201. 400 `{"detail"}` if `t` is outside 0 to the video duration, the label or quality is not in the lists, or the note is over 500 characters; 422 on a malformed body |
| `DELETE /api/jobs/{job_id}/labels/{id}` | 204; 404 if the label is unknown |
| `GET /api/jobs/{job_id}/labels.csv` | The labels as a CSV download (`id,t,label,quality,note,created_at`; notes starting with `=`, `+`, `-` or `@` get a leading `'`) |
| `GET /api/labels.csv` | Every labeled video in one CSV (`hash,filename,id,t,label,quality,note,created_at`), no live job needed. Each label file in `data/labels/` also stores the video's original (sanitized) filename and duration |
| `GET /api/jobs/{job_id}/video` | The job's prepared copy of the video (converted to H.264 when the upload needed it) with HTTP Range support; the player switches to it when the browser cannot decode the local file. 409 until the conversion step is done, 404 if the job or file is gone |
| `GET /uploads/audio/{job_id}/{file}.mp3` | Generated speech clips. Upload paths are never served |

## Configuration (`backend/.env`, see `backend/.env.example`)

| Setting | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_TIMEOUT`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`, `LLM_ENABLE_THINKING`, `LLM_JSON_SCHEMA` | `http://127.0.0.1:8080/v1`, local, 300 s, 500, 0.8, false, true | LLM endpoint and limits |
| `LLAMA_SERVER_PATH`, `LLM_MODEL_PATH`, `LLM_MMPROJ_PATH`, `LLM_CTX` | empty, empty, empty, 8192 | Used by `start.ps1` only, to launch llama-server when nothing answers at `LLM_BASE_URL` |
| `WINDOW_SECONDS`, `FRAMES_PER_WINDOW`, `FRAME_WIDTH` | 10, 6, 896 | Vision windowing |
| `MAX_QUIPS_PER_WINDOW`, `MIN_QUIP_GAP_SEC` | 3, 4.5 | Quip budget (4.5 s is about the speech time of an 8-word line) |
| `TTS_ENGINE`, `PIPER_MODEL_PATH`, `PIPER_LENGTH_SCALE` | edge, models/piper/en_US-lessac-medium.onnx, 0.9 | Speech engine (`edge` or `piper`) and Piper settings |
| `TTS_VOICE`, `TTS_RATE`, `TTS_TIMEOUT_SECONDS`, `TTS_CONCURRENCY`, `TTS_RETRIES`, `TTS_RETRY_DELAY_SECONDS`, `TTS_ENABLED` | en-US-GuyNeural, +10%, 30, 1, 1, 1, true | Speech |
| `MAX_SHIFT_SEC` | 2.0 | Largest delay allowed to avoid audio overlap |
| `LABELS_DIR` | `data/labels` | Where moment labels are stored, one `<sha256 of the video>.json` per clip; git-ignored and kept after jobs expire |
| `FFMPEG_PATH`, `EXPORT_TIMEOUT_SECONDS`, `TRANSCODE_TIMEOUT_SECONDS` | bundled, 1800, 3600 | ffmpeg for mp4 export: empty uses the binary from `imageio-ffmpeg` (installed by requirements.txt); set a path to use your own |
| `EXPORT_DUCK_DB`, `EXPORT_QUIP_LUFS` | -14, -14 | mp4 export: how far the original audio is lowered under a quip, and the loudness each quip is normalized to |
| `TRANSCODE_MAX_SIDE`, `TRANSCODE_MAX_FPS` | 1920, 30 | A converted phone clip is scaled to at most this long side and frame rate; HDR (HLG/PQ) is tone-mapped to SDR |
| `MAX_VIDEO_SECONDS`, `MAX_UPLOAD_MB` | 1800, 2048 | Upload limits (guards, not speed promises) |
| `JOB_TTL_SECONDS`, `JOB_STALE_SECONDS`, `CLEANUP_INTERVAL_SECONDS` | 3600, 1800, 600 | Job expiry; stale-running-job timeout; cleanup interval |
| `CORS_ORIGINS`, `PUBLIC_BASE_URL`, `LOG_LEVEL` | localhost:5173/3000, http://localhost:8000, INFO | Server |

The system prompt is `backend/app/prompts/commentary_system.txt` plus `lion_dance_glossary.txt` (marked DRAFT until reviewed), an optional `examples.txt` (added only when it has real examples) and the chosen style from `prompts/styles/*.txt`. Restart the backend after editing. Each window also gets the last two windows' summaries and recent quips as earlier context.

To compare prompts or styles: list clip paths in `eval/clips.txt`, run `python scripts/eval_commentary.py --styles hype,deadpan` from `backend/` (LLM step only; needs llama-server), rate the rows in `eval/results/<timestamp>.csv`, then `python scripts/eval_commentary.py --summarize` prints the mean rating per prompt hash and style.

## Tests

```powershell
pytest backend/tests -q          # 270 tests: LLM and edge-tts faked, real tiny videos and a real mp3 for the rest; one real Piper test (skipped without Piper)
cd frontend; npm test; npm run lint; npm run build
```
Not covered by automated tests: the real model, the real edge-tts service, browser playback sync (checked by hand, see How it was tested).

## Benchmarks

Measured with `backend/scripts/benchmark.py` on Windows 11, Qwen3.5-9B Q4_K_M via llama-server (35B row as labeled), default settings, edge-tts 7.3.1. Full data and environment: [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md).

| Clip | Length | Processing | Quips (with audio) | Spoken | Overlaps | Shifted / dropped / TTS failed |
|---|---|---|---|---|---|---|
| shorter_test.mp4 | 4.8 s | 66 s | 1 (1) | 3.2 s | 0 | 0 / 0 / 0 |
| hoco_40s.mp4 | 40.2 s | 189 s | 7 (7) | 28.7 s | 0 | 0 / 0 / 0 |
| hoco_40s.mp4, Qwen3.6-35B-A3B (2026-11-03, cold) | 40.2 s | 212 s | 7 (7) | 25.9 s | 0 | 1 / 0 / 0 |

The shorter_test row is from 2026-10-28; the hoco_40s row was re-measured on 2026-11-02 (4 model calls, 0 retries, about 43 s per call). On the same day Qwen3.6-35B-A3B UD-Q3_K_XL took 173 s for the 40 s clip (about 39 s per call). Re-measured on 2026-11-03 on an idle machine: 212 s cold (about 49 s per uncached call), 165 s on a repeat that reused the prompt cache for window 1, and 182 s with speech switched off (`TTS_ENABLED=false`), so speech is not what limits the speed. Both runs used `LLM_JSON_SCHEMA=true` (the default). The earlier run without the fix needed 3 retries in 9 calls (40 s clip: 299 s). hoco2026.MOV (327 s, 6 GB) was rejected by the default 2 GB upload limit in the earlier run and was not re-run.

## How it was tested
- 270 backend tests (LLM and edge-tts faked, Piper faked plus one real synthesis test that is skipped without Piper; real tiny videos and a real mp3), 13 frontend tests (Vitest), frontend lint and build.
- One scripted end-to-end run on 3 real clips plus a repeat (table above) against a real llama-server and the real edge-tts service: all 4 accepted jobs finished, with no overlapping audio and no TTS failures in the timeline.
- Browser playback (sync, seek, pause) checked by hand on 2026-10-28 with hoco_40s.mp4 and the final fix: no problems seen.
- Untested: commentary quality (judged by ear and eye only), clips longer than 40 s, other machines.

## Troubleshooting
- Progress stops and the page says "Live updates lost": on Windows, selecting text in the backend console window (QuickEdit) pauses the server until you press Enter. Press Enter, or turn QuickEdit off in the console properties.
- Job fails with "The AI model rejected the request": llama-server returned an HTTP error (the status is in the backend log); check its console, the model and mmproj files, and `-c`.
- Job fails with "The AI model is not reachable": start llama-server (see Quickstart) and check `LLM_BASE_URL`.
- No audio, "audio unavailable" notice: edge-tts needs internet and `edge-tts>=7.3.1`. Run `python backend/scripts/tts_check.py` (venv active) to synthesize 6 test lines through the app's own TTS code with your current settings; it prints OK/fail with byte sizes, and failures are logged with the exception class, text length, voice, rate and file size. Clips are synthesized one at a time by default (`TTS_CONCURRENCY=1`); "No audio was received" and dropped connections are retried once.
- Every line shows "(no audio)" and the log says "duration unreadable": uvicorn is not running from `backend/venv`. Activate it (prompt shows `(venv)`) and restart.

## Known limitations
- Single user, in-memory jobs, no auth, one job at a time (a second upload gets 409; the LLM server is single-slot). Starting the backend deletes leftover job files (`<job id>_<name>` videos, `<job id>` audio and export folders) in `uploads/`; other files there are left alone.
- Longest clip measured: 40 s (about 4 min of processing). Longer clips, up to `MAX_VIDEO_SECONDS`, are allowed but untested; a 327 s, 6 GB clip (4K 120 fps HDR, about 1.1 GB per minute) is rejected by the default 2 GB upload limit after the whole file has been sent; see `docs/research/large_uploads.md` for options. A 40 s cut of that clip (752 MB) was processed end to end.
- Processing is slow on local hardware: 40 s of video took 189 s (about 4.7x real time) on Qwen3.5-9B, 43 s per model call on average (2026-11-02; 251 s and 54 s on 2026-10-28). Skipped windows leave gaps without commentary; jobs are lost on restart. The 35B-A3B (UD-Q3_K_XL, `-c 8192`) measured 173 s on 2026-11-02 and 212 s cold on 2026-11-03 (5.3x real time) for the same clip; the difference between those days is unexplained.
- Speech needs internet (edge-tts) unless `TTS_ENGINE=piper`. A transient TTS failure is retried once; if edge-tts stays unreachable the app shows the commentary as text with a notice.
- `.avi` and some `.mov` files are accepted by the server but may not play in the browser.
- Runs natively on Windows only; there are no Docker files.

## More
- [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md) contracts and rules, [`docs/DECISIONS.md`](docs/DECISIONS.md) design decisions, [`docs/STATE.md`](docs/STATE.md) what is implemented, [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) measurements, [`docs/TASKS.md`](docs/TASKS.md) roadmap.
