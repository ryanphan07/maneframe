# Maneframe roadmap (v0.1.0 shipped 2026-10-28)

Design decisions are in `docs/DECISIONS.md`; what is implemented is in `docs/STATE.md`.

## Done
- Upload validation, job store, WebSocket progress, background pipeline with cleanup of job files.
- Motion-based crop and windowed frame sampling with OpenCV.
- Vision-LLM commentary through an OpenAI-compatible endpoint (llama-server, multimodal), with tolerant JSON parsing and constrained output.
- edge-tts synthesis with retries, and server-side scheduling so audio never overlaps.
- React player: blob-URL video, quip queue, ducking while a clip plays, seek and pause handling.
- Limits as settings, ETA in progress messages, safety and robustness fixes.
- Benchmark script and results (`docs/BENCHMARKS.md`), browser playback check, demo GIF and video.

## Direction
The goal is a better product for real users: lion dance teams turning performance clips into shareable posts. Honesty rules stay: no canned LLM text, no fake audio.

## Phase 1: shareable output and commentary quality
- mp4 export (done, session 21): the video with the commentary audio mixed in and optional burned-in captions; `GET /api/jobs/{id}/export.mp4`. Follow-ups: try it on a real clip, progress for long renders, output size options.
- Commentary quality (groundwork done, session 22): glossary (DRAFT, needs owner review), `seen` continuity across windows, styles (hype, deadpan, broadcast) and `scripts/eval_commentary.py`. Still to do: fill `examples.txt` with real few-shot examples, run the eval on a live model and compare prompts, models and styles, tune the style texts from the ratings.

- Labeling tool (done, session 25): moment labels in the player, stored by video hash in `data/labels/`, CSV download. Session 27: `GET /api/labels.csv` exports all labeled videos without a live job; label files keep the filename and duration. Follow-up: use the labels as eval ground truth in `scripts/eval_commentary.py`.

## Phase 2: speed and robustness
- 9B vs 35B-A3B throughput table (`FRAME_WIDTH` 640, `FRAMES_PER_WINDOW` 4, `WINDOW_SECONDS` 20, optionally `llama-server -np 2`).
- Sequential crop sampling (done, session 23): the crop-box probe reads its 30 frames in one forward pass; 5.9 s to 1.8 s on hoco_40s.mp4. `sample_window_frames` (done, session 27) seeks once per window and reads forward: identical frames on hoco_40s.mp4 (24 frames), extraction 9.7 s to 5.3 s.
- Progressive results (D4 option C): done in session 28.
- Phone clips (done, session 23): HEVC, other codecs OpenCV may not decode and clips with rotation metadata are transcoded to upright H.264 (session 27: as the job's first step, not in the upload request). Untested on a real phone clip. The player falls back to `GET /api/jobs/{id}/video` when the browser cannot decode the local file (session 27); untested with a real HEVC clip in a browser.
- Export hardening (done, session 23): filter graph via `-filter_complex_script`, quip inputs as relative paths (300 quips tested), and the browser no longer buffers the mp4 (`POST /api/jobs/{id}/export` renders, then the download is a plain link).
- Hardening and tests (done, session 29): transcode progress and timeout, frame-sampling edge cases, Vitest frontend tests, progressive results run live.
- Smaller items: show dropped quips as skipped; keep the volume setting while a clip ducks the video; offline TTS (done in session 32 as `TTS_ENGINE=piper`; D3).

## Phase 3: distribution (undecided)
Local packaging versus a hosted service.

## Out of scope
Auth, persistence or a database, a multi-user queue, S3 or presigned uploads, Docker, deployment, an MCP server, an analytics dashboard.
