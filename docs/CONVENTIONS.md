# Project Conventions

## Project context
Maneframe is a local-first app that turns lion dance videos into humorous, time-synced AI commentary with TTS audio. FastAPI backend (OpenCV frames, OpenAI-compatible multimodal LLM, edge-tts) + React/Vite frontend with WebSocket progress.

Goal: a better product for real users: lion dance teams turning performance clips into shareable posts. Upload a clip, see progress, get commentary with non-overlapping audio, run locally in a few commands; next, export a finished mp4. Clips up to `MAX_VIDEO_SECONDS` (1800) and `MAX_UPLOAD_MB` (2048) are allowed but slow (about 6x real time on the 9B model); see docs/DECISIONS.md D1. Planned work is in `docs/TASKS.md`.

## Environment
- Windows + PowerShell. Native run; there are no Docker files.
- LLM: llama-server (OpenAI-compatible) at `http://127.0.0.1:8080/v1`, multimodal via `--mmproj`. Configure via `backend/.env` (see `backend/.env.example` for LLM_*, WINDOW_SECONDS, FRAMES_PER_WINDOW, FRAME_WIDTH, MAX_QUIPS_PER_WINDOW, MAX_VIDEO_SECONDS).
- Model in use on 2026-10-28: Qwen3.5-9B Q4_K_M (the larger Qwen3.6-35B-A3B is the intended model for real runs). Measured 2026-11-02 (docs/BENCHMARKS.md): about 43 s per LLM call, 189 s for a 40 s clip (35B-A3B UD-Q3_K_XL: 39 s per call, 173 s).
- Start it with: `llama-server -m <gguf> --mmproj <mmproj-F16.gguf> -c 32768 -np 1 -ngl 99 --port 8080 --jinja`
- The system prompt is built by `llm.load_system_prompt(style)` from `backend/app/prompts/commentary_system.txt` (`MAX_QUIPS` is replaced by MAX_QUIPS_PER_WINDOW at load), `lion_dance_glossary.txt` (a leading `DRAFT` line is dropped), `examples.txt` (only if it has non-`#` lines) and `styles/<style>.txt`; the files are read once per process and checked at startup. Each window's user message carries the last 2 windows' `seen` summaries and recent quips as earlier context. `scripts/eval_commentary.py` compares prompts and styles (eval/clips.txt and eval/results/ are git-ignored). The LLM step never falls back to canned text: a window that fails after one retry is skipped and reported (DECISIONS D4); a job where every window fails ends in `error`.
- TTS: edge-tts (needs internet). Failures yield no audio (`audio_url` null), never placeholder files. Settings: TTS_VOICE, TTS_RATE, TTS_TIMEOUT_SECONDS, TTS_CONCURRENCY (1), TTS_START_STAGGER_SECONDS (0.3), TTS_RETRIES, TTS_RETRY_DELAY_SECONDS, MAX_SHIFT_SEC. Retried once: timeouts, connection errors, 5xx, edge-tts NoAudioReceived/WebSocketError; never 401/403. `backend/scripts/tts_check.py` smoke-tests the TTS path.
- mp4 export uses ffmpeg: `FFMPEG_PATH` if set, else the binary bundled with `imageio-ffmpeg` (requirements.txt). Exports are rendered on demand into `uploads/exports/<job id>/`; the ffmpeg command is built by pure functions in `services/export.py`. The filter graph is passed with `-filter_complex_script` (file `filter_graph.txt` in that folder) and quip audio as relative paths, to stay under the Windows command-line limit. The subtitles filter gets a relative file name and ffmpeg runs with that folder as cwd (no Windows path escaping).
- Uploads that are HEVC, otherwise undecodable by OpenCV or carry rotation metadata are transcoded in place to upright H.264 by `services/transcode.py` (needs ffmpeg; skipped without it).
- All settings come from the single shared `settings` object in `backend/app/config.py`. No hardcoded URLs or paths elsewhere.

## Commands
(If any of these is wrong, fix this section.)
Run from the repo root in PowerShell unless noted. Tests (215) verified 2026-11-02; lint and build verified 2026-10-29, `uvicorn` last verified 2026-10-27. edge-tts must be >= 7.3.1 (re-run `pip install -r requirements-dev.txt` after pulling).
- Backend setup (once): `cd backend; python -m venv venv; venv\Scripts\Activate.ps1; pip install -r requirements-dev.txt` (runtime deps are in requirements.txt).
- Backend tests: `pytest backend/tests -q` (works from the repo root or from `backend/`).
- Backend run: `cd backend; uvicorn app.main:app --port 8000`. `backend/.env` is resolved relative to `config.py`, so the working directory does not matter for `.env`. `CORS_ORIGINS` (JSON list) is configurable there.
- Frontend setup (once): `cd frontend; npm install`. Optional `frontend/.env` with `VITE_API_BASE` (default http://localhost:8000); see `.env.example`.
- Frontend: `cd frontend; npm run lint; npm run build; npm run dev` (dev server on http://localhost:5173).

## Documentation
- Update docs once at the end of each batch of work, not after every edit:
  - `STATE.md`: what is implemented
  - `TASKS.md`: roadmap
  - `README.md`: what it is, architecture, quickstart, route list, known limitations
- Docs must match the code. Mark anything unverified as untested.
- No pasted chat text, citation markers, or meta commentary in docs.

## API & WebSocket Contract
- `POST /api/upload` (multipart `file`, optional form field `style`: `hype` default, `deadpan`, `broadcast`; the allowed values are the files in `prompts/styles/`, anything else is a 400) returns JSON including `job_id`. The job runs in the background. The style is stored on the job and returned as `result.style`.
- `WS /ws/analyze/{job_id}` is server-to-client only. Every message is:
  ```json
  {
    "status": "uploading" | "extracting" | "analyzing" | "synthesizing" | "completed" | "error",
    "progress": 0-100,
    "message": "optional human-readable detail, required when status is 'error'"
  }
  ```
- On subscribe the server sends the current state first, so reconnects are safe. Unknown job: send an error message, close with code 4404.
- The `completed` message carries no result. Fetch it from `GET /api/jobs/{job_id}`, which returns `{job_id, status, progress, message, result}`.
- Frontend listeners switch on `msg.status` only (never `msg.phase`, never fall back to `msg.message`).
- `POST /api/jobs/{job_id}/export?captions=1|0` renders the mp4 and returns `{"url"}` for `GET /api/jobs/{job_id}/export.mp4`; errors are JSON `{"detail"}`.
- `GET|POST /api/jobs/{job_id}/labels`, `DELETE /api/jobs/{job_id}/labels/{id}` and `GET /api/jobs/{job_id}/labels.csv` manage moment labels (`{id, t, label, quality, note, created_at}`) for eval and training data; see README for the full shapes. Labels are stored in `LABELS_DIR` (`data/labels/<sha256 of the video>.json`, git-ignored), keyed by content hash so they outlive the job; the routes need the job and its kept video, so they 404 once the job expires. `t` must lie within the video duration.
- HTTP errors: `HTTPException` with `{"detail": "<clear message>"}`.

## Canonical Processing Phases
```
idle -> uploading -> extracting -> analyzing -> synthesizing -> completed
                                                               -> error
```
- `idle` exists only as the frontend's initial state; the backend never sends it.
- Use these exact strings everywhere (state values, WebSocket payloads, UI labels). Don't add or reorder phases without updating this list.

## Backend Rules
- Any upload/processing failure must delete the job's temporary/uploaded files before raising or finishing.
- Client-facing error messages are generic. Log details server-side with `logger.exception`. Never log base64 frame data or secrets.
- Never trust client-supplied paths. The server owns all file paths. `job_id` is the `video_id` for storage (video, frames, audio).
- Serve only the audio folder statically; never mount the videos folder or expose a raw upload path. The one exception is `GET /api/jobs/{job_id}/video`: it serves the job's own prepared copy (converted to H.264 when needed, so not the untouched upload) with HTTP Range support, as a playback fallback for browsers that cannot decode the local file. The path comes from the job record, never from the request; the route answers 409 until the conversion step has finished. Rendered mp4 exports are downloadable only through `GET /api/jobs/{job_id}/export.mp4`.
- The source video is kept after a successful job (mp4 export reads it) and deleted with the job: on failure or cancel immediately, on success when the job passes `JOB_TTL_SECONDS` (`pipeline.purge_expired_jobs`, run by the cleanup loop and before each upload). Rendered exports live in `uploads/exports/<job id>/` and go with the job.
- Validation (filename sanitization, extension/size/duration limits, corruption checks) happens before a write is considered final.

## Testing
- Tests assert behavior. Mock only external boundaries (LLM client, TTS, OpenCV I/O), not the logic under test.
- Run backend tests and frontend lint/build before every commit. One logical change per commit.

## Timing
- Spoken-line scheduling is server-side (`commentary.schedule_quips`). Measured TTS speed: about 1.8 words per second including padding (8 words = 4.5 s). Keep `MIN_QUIP_GAP_SEC` (4.5) and the prompt's spacing and length rules (4.5 s apart, 10 words) consistent with that. Quip text is never truncated.
- Quip timestamps in results are rounded to 2 decimals.

## Library / API Pins
- **react-toastify**: use string literals for position, e.g. `toast.error(msg, { position: "top-right" })`. Do NOT use `toast.POSITION.TOP_RIGHT` (deprecated).

## File & Folder Layout
- Backend routes/entrypoint: `backend/app/main.py`
- Backend services: `backend/app/services/` (e.g. `jobs.py`, `commentary.py`, `llm.py`, `tts.py`, `video_processor.py`)
- Moment labels: `backend/app/services/labels.py`; stored under `data/` (git-ignored)
- Backend settings/schemas: `backend/app/config.py`, `backend/app/schemas/`
- Backend tests: `backend/tests/`; dev scripts: `backend/scripts/` (`beat_sync.py` is a research prototype, see docs/research/beat_sync.md; it needs librosa and matplotlib from requirements-dev.txt)
- Frontend app/state: `frontend/src/App.jsx`; components: `frontend/src/components/`
- Docs: root `README.md`; in `docs/`: `STATE.md`, `TASKS.md`, `DECISIONS.md`, `BENCHMARKS.md`, `CONVENTIONS.md`.