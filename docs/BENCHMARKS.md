# Benchmarks

Measured by `backend/scripts/benchmark.py` against a running backend. Each run appends a dated section; earlier sections are never overwritten.

## 2026-10-28 17:07

### Environment

- Date: 2026-10-28 17:07
- Git commit: 36c2edb (uncommitted changes)
- Model (GET /v1/models): C:\AI\models\Qwen3.5-9B-Q4_K_M.gguf
- edge-tts: 7.3.1
- Python: 3.12.10
- OS: Windows-11-10.0.26200-SP0
- Settings (as loaded by this script from backend/.env; the server must have been started from the same folder): LLM_BASE_URL=http://127.0.0.1:8080/v1, LLM_MODEL=local, LLM_TIMEOUT=300.0, LLM_ENABLE_THINKING=False, LLM_MAX_TOKENS=500, LLM_TEMPERATURE=0.8, WINDOW_SECONDS=10.0, FRAMES_PER_WINDOW=6, FRAME_WIDTH=896, MAX_QUIPS_PER_WINDOW=3, MIN_QUIP_GAP_SEC=4.5, TTS_VOICE=en-US-GuyNeural, TTS_RATE=+10%, TTS_ENABLED=True, TTS_TIMEOUT_SECONDS=30.0, TTS_CONCURRENCY=1, TTS_START_STAGGER_SECONDS=0.3, TTS_RETRIES=1, MAX_SHIFT_SEC=2.0, MAX_VIDEO_SECONDS=1800.0

### Results

| Run | Clip (length / size) | Total s | Extract / analyze / synth s | Final status | Quips (total / audio / no audio) | Spoken s | Audio overlaps | LLM calls (incl. retries) / avg s / max s | Avg prompt / completion tokens | Shifting / Dropping / TTS failed |
|---|---|---|---|---|---|---|---|---|---|---|
| shorter_test.mp4 | 4.8 s / 12.3 MB | 124.3 | 12.0 / 110.3 / 2.0 | completed | 1 / 1 / 0 | 4.0 | 0 | 2 / 54.3 / 54.4 | 2972 / 129 | 0 / 0 / 0 |
| hoco2026.MOV | 326.6 s / 6044.9 MB | n/a | n/a / n/a / n/a | error: upload rejected 400: {"detail":"File size exceeds the maximum limit of 2048MB."} | n/a | n/a | n/a | n/a | n/a | n/a |
| hoco_40s.mp4 | 40.2 s / 101.7 MB | 298.6 | 12.0 / 278.5 / 8.0 | completed | 7 / 7 / 0 | 27.5 | 0 | 5 / 53.6 / 61.5 | 2961 / 136 | 0 / 0 / 0 |
| shorter_test.mp4 - warm (prompt cache) | 4.8 s / 12.3 MB | 50.1 | 12.0 / 38.1 / n/a | completed | 1 / 1 / 0 | 3.1 | 0 | 2 / 18.3 / 19.0 | 2972 / 142 | 0 / 0 / 0 |

Phase seconds come from polling every 2 s (about +/-2 s). Total is wall clock from upload accepted to the first poll that saw `completed`/`error`. Overlap = consecutive audio quips where timestamp + duration_sec exceeds the next timestamp by more than 0.05 s.
Window, token and Shifting/Dropping/TTS failed columns are from the backend log of that run (not committed); n/a means the job was not found in the log.

Per-window LLM seconds:

- shorter_test.mp4: 54.4s, 54.3s
- hoco_40s.mp4: 55.2s, 42.8s, 50.2s, 58.5s, 61.5s
- shorter_test.mp4 - warm (prompt cache): 17.6s, 19.0s

Notes on this run (written by hand after it; nothing was fixed or tuned during the run):
- LLM calls are one per window plus retries. shorter_test needed 1 window but made 2 calls and hoco_40s had 4 windows but 5 calls, because the model replied with two JSON objects (`{"seen": ...}` then `{"t": ..., "text": ...}`) or fenced JSON without a `quips` list ("unusable reply on attempt 1" in the log), which triggers the one JSON-nudge retry. That retry roughly doubled the cold shorter_test time. 3 of 9 calls hit it.
- hoco2026.MOV (326.6 s, 6045 MB) was rejected at upload by the default `MAX_UPLOAD_MB=2048`; no processing happened. No long-clip number exists.
- The warm run (same 4.8 s clip, same server, llama-server prompt cache) took 17.6 s and 19.0 s per call versus 54 s cold, 50.1 s total versus 124.3 s.
- The 12.0 s extracting-to-analyzing step is identical in all four runs; not investigated. Session 23 timed the extracting work alone (`get_video_duration` + `compute_crop_box`) on hoco_40s.mp4 (1080p, 30 fps, H.264), no model needed: 5.5 to 6.1 s with 30 seeks, 1.6 to 2.1 s with the sequential pass, identical crop box (0.109, 0.295, 0.885, 0.927).
- The server for this run listened on port 8001 because port 8000 was held by another uvicorn process that the benchmark did not start.
- Quality of the commentary and the audio were not judged here; that needs eyes and ears.

## 2026-10-28 20:41

### Environment

- Date: 2026-10-28 20:41
- Git commit: 316383a
- Model (GET /v1/models): C:\AI\models\Qwen3.5-9B-Q4_K_M.gguf
- edge-tts: 7.3.1
- Python: 3.12.10
- OS: Windows-11-10.0.26200-SP0
- Settings (as loaded by this script from backend/.env; the server must have been started from the same folder): LLM_BASE_URL=http://127.0.0.1:8080/v1, LLM_MODEL=local, LLM_TIMEOUT=300.0, LLM_ENABLE_THINKING=False, LLM_MAX_TOKENS=500, LLM_TEMPERATURE=0.8, WINDOW_SECONDS=10.0, FRAMES_PER_WINDOW=6, FRAME_WIDTH=896, MAX_QUIPS_PER_WINDOW=3, MIN_QUIP_GAP_SEC=4.5, TTS_VOICE=en-US-GuyNeural, TTS_RATE=+10%, TTS_ENABLED=True, TTS_TIMEOUT_SECONDS=30.0, TTS_CONCURRENCY=1, TTS_START_STAGGER_SECONDS=0.3, TTS_RETRIES=1, MAX_SHIFT_SEC=2.0, MAX_VIDEO_SECONDS=1800.0

### Results

| Run | Clip (length / size) | Total s | Extract / analyze / synth s | Final status | Quips (total / audio / no audio) | Spoken s | Audio overlaps | LLM calls (incl. retries) / avg s / max s | Avg prompt / completion tokens | Shifting / Dropping / TTS failed |
|---|---|---|---|---|---|---|---|---|---|---|
| hoco_40s.mp4 | 40.2 s / 101.7 MB | 250.8 | 14.2 / 228.5 / 8.1 | completed | 6 / 6 / 0 | 22.8 | 0 | 4 / 54.9 / 57.5 | 2950 / 116 | 0 / 0 / 0 |
| shorter_test.mp4 | 4.8 s / 12.3 MB | 66.1 | 12.0 / 52.1 / 2.0 | completed | 1 / 1 / 0 | 3.2 | 0 | 1 / 52.0 / 52.0 | 2962 / 110 | 0 / 0 / 0 |

Phase seconds come from polling every 2 s (about +/-2 s). Total is wall clock from upload accepted to the first poll that saw `completed`/`error`. Overlap = consecutive audio quips where timestamp + duration_sec exceeds the next timestamp by more than 0.05 s.
Window, token and Shifting/Dropping/TTS failed columns are from `maneframe_bench.log`; n/a means the job was not found in the log.

Per-window LLM seconds:

- hoco_40s.mp4: 53.9s, 57.5s, 52.5s, 55.5s
- shorter_test.mp4: 52.0s

Notes on this run (with the multi-object JSON fix; nothing was tuned during the run):
- `LLM_JSON_SCHEMA=true` (the default; not in the settings list above because the script does not print it). Unusable-reply retries: 0 ("unusable reply" appears 0 times in the backend log). LLM calls equal windows: 4 for hoco_40s, 1 for shorter_test. The 2026-10-28 17:07 run had 3 retries in 9 calls.
- hoco_40s: 250.8 s versus 298.6 s before (about 6.2x real time versus 7.4x); shorter_test cold: 66.1 s versus 124.3 s. Per-call time is unchanged at about 54 s; the gain is the removed retries. Quips went 7 to 6 (model output varies run to run).
- hoco2026.MOV and the warm repeat were not run this time. Model: Qwen3.5-9B Q4_K_M, not the 35B-A3B.
- Quality of the commentary and the audio were not judged here.
