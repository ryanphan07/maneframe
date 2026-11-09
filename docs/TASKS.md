# Maneframe roadmap (v0.1.0 shipped 2026-10-28; v0.2.0 tagged 2026-11-04)

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
- mp4 export (done, session 21): the video with the commentary audio mixed in and optional burned-in captions; `GET /api/jobs/{id}/export.mp4`. Session 34: quips normalized and the original ducked deeper (verify report). Follow-ups: try it on a real clip (done on hoco_40s and a phone cut), progress for long renders. Output size options (done, session 49): `aspect=9:16` (first version: static motion-box crop, 1080x1920 or source height, captions in the lower third; session 51: automatic per-segment framing, crop window that follows the motion or the whole frame over a blur, captions placed off the performers) and `start`/`end` trim, with controls next to "Download video"; rendered on hoco_40s (35B, hype): 9:16 full clip 10.7 s, 9:16 15 s trim 6.8 s. Not done: other aspect ratios, a manual framing override, upload to social apps.
- Commentary quality (groundwork done, session 22): glossary (DRAFT, needs owner review), `seen` continuity across windows, styles (hype, deadpan, broadcast) and `scripts/eval_commentary.py`. Session 36-38: eval set, blind grounding review and four arms (none beat the baseline); the owner's glossary and examples are in the prompt (session 38: grounding unchanged at about 55%, about 25% slower; named-move lines are the new risk). Session 45: arm r2 (arm r rules plus a micro-motion ban) was not a winner (51% against 52%, unread lines 20 to 4 but named-move and pose errors rose; see grounding.md). Still to do: a cheaper facts call, a short commentary glossary if more prompt room is needed.

- Humor arm s (session 39, eval only): slang and pop-culture voice, `backend/scripts/eval_arms/s_slang.txt`. Grounding 68% against 53% on a 30-line drift sample (`docs/research/grounding.md`); the owner humor sheet `logs/grounding/owner_humor/pool_s39/` is superseded by the single pool_s40 sheet (see arm s2). Fixed rule: arm s replaces `hype.txt` only if the owner mean on its lines is at least 0.7 above the anchors in the same sheet and grounding is no more than 10 points below the drift sample. Session 54 verdict: owner mean 1.88 against anchors 2.10, not a winner; `hype.txt` unchanged.
- Humor arm s2 (session 40, eval only): arm s with a rotating per-window option list (`--rotate-terms backend/scripts/eval_arms/s_terms.txt`, rules `s2_slang.txt`): references in 38% of lines (arm s 1%), slang repeats about 5x fewer, grounded 55% against 50% on the drift sample. The owner rates ONE sheet, `logs/grounding/owner_humor/pool_s40/` (25 arm s, 25 s2, 10 anchors; replaces pool_s39). Rules are in `docs/research/grounding.md` (session 40); Session 54 verdict: owner mean 2.36 against anchors 2.10 (needed 2.80), not a winner; `hype.txt` unchanged and the rotation stays eval-only. The pool_s38 sheet is rated.

- Labeling tool (done, session 25): moment labels in the player, stored by video hash in `data/labels/`, CSV download. Session 27: `GET /api/labels.csv` exports all labeled videos without a live job; label files keep the filename and duration. Follow-up: use the labels as eval ground truth in `scripts/eval_commentary.py`.

## Phase 2: speed and robustness
- 9B vs 35B-A3B throughput table (`FRAME_WIDTH` 640, `FRAMES_PER_WINDOW` 4, `WINDOW_SECONDS` 20, optionally `llama-server -np 2`).
- Sequential crop sampling (done, session 23): the crop-box probe reads its 30 frames in one forward pass; 5.9 s to 1.8 s on hoco_40s.mp4. `sample_window_frames` (done, session 27) seeks once per window and reads forward: identical frames on hoco_40s.mp4 (24 frames), extraction 9.7 s to 5.3 s.
- Progressive results (D4 option C): done in session 28.
- Per-call timing (done, session 46): `services/llm.py` logs llama-server's `timings`. 82% of a call is prompt processing (about 2440 image/window tokens at 65 tokens/s; the 3716-token system prompt is cached from call 2); `-ub 2048`, `-fa on` and `--cache-reuse` (disabled for multimodal) each gave 3% or less, so no flag adopted (`docs/BENCHMARKS.md`). Further speed needs fewer image tokens, a quality decision (session 53 measured 4 frames per window: 0.76 to 0.81x time, grounding -3 points, notable lines 21% to 8%; not adopted, default stays 6).
- Prefill speed (done, session 47): diagnosed, not a setup problem. The Radeon 860M iGPU runs everything (41/41 layers, KV, experts, mmproj) on Vulkan, so about 65 tokens/s is the hardware limit. A newer official Vulkan build (b12131) changed s/call by -11% and +7%, not adopted (`docs/BENCHMARKS.md`). Faster needs fewer image tokens or a discrete GPU.
- Phone clips (done, session 23; session 34: a real 4K 120 fps HDR iPhone cut ran end to end, conversion capped at 1080p/30 fps and tone-mapped, 25+ min down to about 1 min): HEVC, other codecs OpenCV may not decode and clips with rotation metadata are transcoded to upright H.264 (session 27: as the job's first step, not in the upload request). Session 34 ran a real HEVC phone cut end to end; that is the only real phone clip so far. The player falls back to `GET /api/jobs/{id}/video` when the browser cannot decode the local file (session 27); untested with a real HEVC clip in a browser.
- Export hardening (done, session 23): filter graph via `-filter_complex_script`, quip inputs as relative paths (300 quips tested), and the browser no longer buffers the mp4 (`POST /api/jobs/{id}/export` renders, then the download is a plain link).
- Hardening and tests (done, session 29): transcode progress and timeout, frame-sampling edge cases, Vitest frontend tests, progressive results run live.
- Smaller items: show dropped quips as skipped; keep the volume setting while a clip ducks the video; offline TTS (done in session 32 as `TTS_ENGINE=piper`; D3; session 39 added `TTS_ENGINE=kokoro` with af_nicole; model files in the git-ignored `models/kokoro/`, setup in the README).

- Large uploads (`docs/research/large_uploads.md`): a 5 min iPhone 4K clip is 6 GB. Option 1 done and tested: a Content-Length above MAX_UPLOAD_MB plus 1 MiB is a 413 before the body is read, and the client checks the file against `max_upload_mb` from `/api/styles` first. Option 3 (streaming upload with a higher cap) is open and needs owner sign-off because it changes the upload route.
- H.264 size and HDR (done, session 43): an H.264 upload whose long side is above `TRANSCODE_MAX_SIDE` or that is HLG/PQ HDR goes through the same conversion; frame rate alone is no trigger. Tested on tiny generated clips (over max side, HLG, 1080p30 untouched); not run on a real 4K H.264 clip.

## Phase 3: distribution
Local only, no hosted service (D6). Open: how to package it (installer, portable folder) and voice options beyond edge-tts (D7).

## Out of scope
Auth, persistence or a database, a multi-user queue, S3 or presigned uploads, Docker, deployment, an MCP server, an analytics dashboard.
