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
- hoco2026.MOV (326.6 s, 6045 MB) was rejected at upload by the default `MAX_UPLOAD_MB=2048`; no processing happened. No long-clip number exists. Session 34: a 40 s stream-copy cut of it (752 MB, 3840x2160 120 fps HLG HEVC) converted in about 1 min after the transcode fix (25+ min before, extrapolated from progress; output 1080p 30 fps SDR) and was analyzed end to end; see docs/research/verify_2026-11-03.md.
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

## 2026-11-02 22:13: 9B vs 35B-A3B, live verification (session 26)

Git commit ea1d2b8. Clip: hoco_40s.mp4 (40.2 s, 101.7 MB), style hype, default settings (same as the 2026-10-28 section), edge-tts 7.3.1, uvicorn started by hand on port 8000, llama-server with `-np 1 -ngl 99 --port 8080 --jinja`. Baseline for comparison is the 2026-10-28 20:41 row: 251 s, 54.9 s per window, 2950 prompt tokens.

| Model | llama-server `-c` | Total s | s/window (4 windows) | Prompt tokens (avg) | Quips (audio) | Retries | Skipped | Failures (TTS / LLM) |
|---|---|---|---|---|---|---|---|---|
| Qwen3.5-9B Q4_K_M | 32768 | 188.5 | 43.1 (45.5, 40.9, 43.0, 43.1) | 3560 | 7 (7) | 0 | 0 | 0 / 0 |
| Qwen3.6-35B-A3B UD-Q3_K_XL | 8192 | 172.8 | 38.5 (45.2, 36.6, 36.8, 35.5) | 3582 | 7 (7) | 0 | 0 | 0 / 0 |

Notes:
- Both runs: 4 LLM calls for 4 windows, 0 "unusable reply" lines in the backend log, no audio overlaps. The benchmark table row for the 35B shows 1 shift, but that line (t=37.62 to 37.88, 22:29:42) was written by the eval_commentary run that followed and is attributed to the benchmark job only because the log has no job id; the benchmark job itself shifted nothing. The later 35B smoke job shifted one quip by 0.41 s (25.83 to 26.24). Spoken seconds: 28.7 (9B), 31.7 (35B).
- Prompt tokens are about 600 higher than the 2026-10-28 baseline (3560 vs 2950), from the prompt and context changes since then, yet the 9B took 43.1 s per window against 54.9 s. Not investigated; llama-server build and GPU state may differ from the earlier run (the 2026-10-28 build is not recorded).
- The 35B fits `-c 8192` for these prompts (about 3600 prompt tokens plus 140 completion tokens per call). The 35B is a mixture-of-experts model with about 3B active parameters, which is the likely reason it is not slower than the 9B; not measured separately.
- `eval_commentary.py` on the same clip, styles hype and deadpan (4 windows each): 9B wrote 23 quips (11 hype, 12 deadpan), 35B wrote 24 (12 and 12). Quality was not rated here.
- API smoke test, both models: upload completed (162 s 9B, 135 s 35B including TTS, so faster than the benchmark pass because of the prompt cache); POST export returned a url; the mp4 is 40.17 s, 1920x1080 H.264 (High) plus AAC LC 48 kHz stereo; a frame extracted while a quip was active shows the caption at the bottom (looked at: 9B at 12 s, 35B at 17 s); adding, listing, deleting and CSV of a label worked. The first label attempt returned 422 because the smoke script sent `quality` as a number; the API takes the strings in `qualities` ("good", "needs work", "none"), which is by design.
- Smoke-test labels were deleted afterwards. Models were not changed; the default stays the 9B.


## 2026-11-03: Qwen3.6-35B-A3B re-measure, TTS cost (session 31)

Git commit 012f23c. Clip hoco_40s.mp4 (40.2 s, 101.7 MB), style hype, default settings, Qwen3.6-35B-A3B UD-Q3_K_XL at `-c 8192` (`-np 1 -ngl 99 --jinja`, llama-server b10679 Vulkan, started by hand), uvicorn started by hand, `benchmark.py` with `--log`. Before the runs no other heavy process was running: top CPU load was 0.8% (the machine had two leftover `find /` searches from earlier sessions, each using a full core; they were stopped first). The model was loaded once and the three runs were back to back, so runs 2 and 3 had the llama-server prompt cache from the earlier runs.

| Run | Total s | Analyze s | Window s | Prompt tokens (avg) | Quips (audio) | Shifting / Dropping / TTS failed |
|---|---|---|---|---|---|---|
| 1 (cold) | 211.9 | 203.8 | 51.2, 48.9, 49.3, 47.3 | 3588 | 7 (7) | 1 / 0 / 0 |
| 2 | 165.4 | 157.3 | 15.0, 45.4, 44.3, 46.0 | 3564 | 7 (7) | 1 / 0 / 0 |
| 3, `TTS_ENABLED=false` | 181.7 | 175.6 | 15.5, 52.8, 48.9, 50.8 | 3571 | 6 (0) | 0 / 0 / n/a |

- All runs: 4 LLM calls for 4 windows, no overlaps. Run 3 ended "Commentary ready (audio unavailable)" with no audio, as intended; there is no synthesizing phase.
- Window 1 in runs 2 and 3 took 15 s against about 50 s in run 1: the first window's prompt and frames are identical across runs, so llama-server reused its cache. The other windows are not cached.
- Run 3 (no TTS) was not faster than run 2 (181.7 s against 165.4 s), and its uncached windows were as slow as run 1's (about 49 to 53 s). So TTS is not what slows the windows here; per-call time varies by about 10 s between runs and one run each does not separate that from noise. TTS adds about 2 s after the last window plus the work done in the background while later windows run.
- Session 26 measured the same model at 172.8 s (38.5 s per window) and session 29 at 221.5 s (about 52 s per window). Today's uncached windows (45 to 53 s) match session 29, not session 26. The cause of the session 26 speed is not established.
- Run 1 is the figure to quote for a cold start: 211.9 s, about 5.3x real time. Not measured: the 9B on this build, quality of the commentary or audio.


## 2026-11-03: edge-tts against Piper (session 32)

Clip hoco_40s.mp4 (40.2 s, 101.7 MB), style hype, default settings except `TTS_ENGINE`, Qwen3.6-35B-A3B UD-Q3_K_XL at `-c 8192` (llama-server b10679 Vulkan, started by hand), uvicorn restarted for each run, `benchmark.py` with `--log`. Piper 1.8.0 (piper-tts) with the `en_US-lessac-medium` voice, `PIPER_LENGTH_SCALE=0.9`, CPU only. Runs back to back in the order edge, piper, edge, piper; the model stayed loaded, so runs 2 to 4 had llama-server's prompt cache for window 1.

| Run | Engine | Total s | Analyze s | Synthesizing phase s | Avg LLM s per call | Quips (audio) | Spoken s | TTS failed |
|---|---|---|---|---|---|---|---|---|
| 1 | edge | 148.2 | 142.2 | 2.0 | 35.2 | 7 (6) | 23.8 | 1 |
| 2 | piper | 128.2 | 124.2 | n/a (under the poll interval) | 30.5 | 6 (6) | 17.9 | 0 |
| 3 | edge | 134.2 | 120.2 | 10.0 | 29.5 | 8 (8) | 33.8 | 0 |
| 4 | piper | 120.2 | 116.2 | n/a (under the poll interval) | 28.6 | 6 (6) | 17.5 | 0 |

TTS alone (the app's own `generate_audio_clip`, the same 7 fixed lines of 7 to 9 words, one at a time, no model running; two passes each, alternating):

| Engine | Pass | Per clip s | Total s | Spoken s |
|---|---|---|---|---|
| edge | 1 | 1.05, 3.61, 1.51, 1.06, 1.27, 1.55, 1.55 | 11.60 | 22.1 |
| piper | 1 | 1.87 (voice load), then 0.05 to 0.07 | 2.23 | 16.6 |
| edge | 2 | 0.80 to 1.07 | 6.13 | 22.1 |
| piper | 2 | 0.04 to 0.06 | 0.39 | 16.6 |

- Piper's synthesis cost is negligible next to the model: about 0.05 s per line after a one-time voice load of about 2 s on the first line, against 0.8 to 3.6 s per line for edge-tts (network). Total job time is dominated by the 4 uncached model calls (about 30 to 50 s each), so the job-time gap in the first table (148 and 134 s against 128 and 120 s) is mostly model-time variation between runs (analyze s differs by 20 s between the two edge runs), not TTS; TTS time in the pipeline overlaps with later windows, so only the part after the last window shows as "synthesizing" (2 s and 10 s with edge, not visible with piper).
- Run 1 lost one clip to edge-tts ("No audio was received", retried once, failed again): quip 7 had text only. Piper had no failures in 2 runs, 12 clips.
- Piper speaks about 25% less audio for the same text (16.6 s against 22.1 s), so its voice is faster even at `PIPER_LENGTH_SCALE=0.9`; the scheduler uses the measured duration, so overlaps are unaffected (0 overlaps in all 4 runs). The quips differ between runs (the model writes new lines each time), which is why "Spoken s" in the first table is not comparable between engines.
- Not measured: voice quality (subjective; the lessac voice is a flat, clear American voice, edge's GuyNeural is more expressive), other Piper voices, `TTS_ENGINE=piper` through the browser player, a wav in the mp4 export (export reads the file with ffmpeg, which decodes wav; the export tests use mp3).


## 2026-11-04: Kokoro (af_nicole) end to end, and Kokoro against edge-tts (session 39)

Kokoro 0.5.0 (`kokoro-onnx`, CPU onnxruntime), `kokoro-v1.0.onnx` (326 MB) with `voices-v1.0.bin`, voice `af_nicole`, speed 1.0, no gain. Job: hoco_40s.mp4 (40.2 s), hype, Qwen3.6-35B-A3B UD-Q3_K_XL at `-c 8192` started by `start.ps1` (llama-server on Vulkan and the backend share the machine), `benchmark.py`. One run, so run-to-run noise (about 10 s per call, see the 2026-11-03 sections) applies.

| Run | Engine | Total s | Extract / analyze / synth s | Quips (audio) | Spoken s | Overlaps | Shifting / Dropping / TTS failed |
|---|---|---|---|---|---|---|---|
| 1 | kokoro af_nicole | 215.1 | 6.4 / 204.7 / 4.0 | 6 (6) | 24.8 | 0 | 0 / 0 / 0 (counted by hand in the log) |

Seconds per line in the job (from the `TTS kokoro` log line), against the audio each line produced:

| Line t | 2.50 | 9.17 | 14.17 | 20.83 | 30.85 | 37.62 |
|---|---|---|---|---|---|---|
| Words | 10 | 9 | 9 | 9 | 7 | 6 |
| Synthesis s | 5.58 (includes the one-time model load) | 2.94 | 2.31 | 2.42 | 2.11 | 1.89 |
| Audio s | 4.95 | 4.69 | 4.07 | 4.07 | 3.50 | 3.52 |

So 1.9 to 2.9 s per line while llama-server is working, about 0.5 of real time, and 3 to 4 s extra once for the load. The model windows took 68, 46, 42 and 43 s (prompt 6000 to 6160 tokens). The benchmark script reads `logs/backend.log` but uvicorn writes its log to `backend.log.err` under `start.ps1`, so its Shifting/Dropping/TTS failed and token columns were n/a; the counts above are from the `.err` file (no Shifting, Dropping or TTS failed lines).

The app's tts_check lines (6 lines, 37 words), TTS alone, nothing else running, edge-tts GuyNeural +10% against Kokoro:

| Voice | Integrated LUFS (peak dBFS) | Audio s | Words/s (audio only) | Synthesis s per line |
|---|---|---|---|---|
| edge GuyNeural | -19.6 (-1.4) | 17.54 | 2.11 | 1.0 to 1.2 |
| kokoro af_nicole, full | -21.0 (-2.7) | 18.05 | 2.05 | 1.2 to 1.8 (3.8 for the first, model load) |
| kokoro af_nicole, int8 | -21.4 (-2.5) | 18.28 | 2.02 | 1.3 to 1.5 (3.9 for the first) |

- Loudness: nicole is 1.4 dB quieter than GuyNeural, under the 3 dB bar, so no gain is applied (`KOKORO_GAIN_DB=0`). Per line nicole varied -20.4 to -21.5, GuyNeural -18.3 to -21.3. The "whisper" is therefore not a level difference in LUFS; whether it sounds thin or breathy is a listening question (see not verified).
- Speech rate: nicole at speed 1.0 is 2.05 words/s of audio (the job: 50 words in 24.8 s, 2.02), the same as edge's 2.11 raw. CONVENTIONS' 1.8 words/s counts padding. Both are above 1.5, so `MIN_QUIP_GAP_SEC` (4.5) and the 10-word limit stay as they are; nothing was changed.
- int8 (92 MB) is not faster than the full model on this CPU (1.3 to 1.5 s against 1.2 to 1.8 s per line), is 0.4 dB quieter and 1.3% slower in speech. The full model stays the default; int8 listening was not done (no way to listen in the session).
- mp4 export path (acompressor, then loudnorm to -14 LUFS, then alimiter), applied to the six nicole clips of the job: speech goes from -21.4 to -22.1 LUFS to -14.4 to -15.0 LUFS (peaks -1.4 to -1.5 dBFS, +7 dB). The quietest 100 ms of each clip (breath and room floor) goes from -82.5 to -84.9 dBFS to -68.3 to -71.3 dBFS (+14 dB), so the floor sits about 54 dB below the speech and about 7 dB closer to it than before. That is far below audibility next to the ducked original (-14 dB); no breath-noise blow-up. edge's mp3 has digital silence between words (-180 dBFS), so it has no floor to lift. Measured on the clips, not by listening to the finished mp4.
- Phonemes (kokoro-onnx's default espeak-ng phonemizer): Fanum "fˈænəm", Labubu "lˈæbuːbˌuː", NPC letter by letter, unc "ˈʌŋk", cooked "kˈʊkt", aura farming "ˈɔːɹə fˈɑːɹmɪŋ". None looks wrong; misaki was not tried.

## 2026-11-05 02:24 (long clip: hoco2026, 327 s, Qwen3.6-35B-A3B)

### Environment

- Date: 2026-11-05 02:24
- Git commit: a32af2a plus the uncommitted benchmark.py timeout/first-partial change (committed afterwards)
- Model: Qwen3.6-35B-A3B-UD-Q3_K_XL with mmproj-Qwen3.6-35B-F16, llama-server on port 8090, LLM_CTX 8192 (the script queried 8080, which Docker holds, so its own model lookup failed)
- edge-tts: 7.3.1
- Python: 3.12.10
- OS: Windows-11-10.0.26300-SP0
- Settings (as loaded by this script from backend/.env; the server must have been started from the same folder): LLM_BASE_URL=http://127.0.0.1:8090/v1 (set by environment variable for the backend; the script printed the .env value), LLM_MODEL=local, LLM_TIMEOUT=300.0, LLM_ENABLE_THINKING=False, LLM_MAX_TOKENS=500, LLM_TEMPERATURE=0.8, WINDOW_SECONDS=10.0, FRAMES_PER_WINDOW=6, FRAME_WIDTH=896, MAX_QUIPS_PER_WINDOW=3, MIN_QUIP_GAP_SEC=4.5, TTS_ENGINE=kokoro, TTS_VOICE=en-US-GuyNeural, KOKORO_VOICE=af_nicole, KOKORO_SPEED=1.0, TTS_RATE=+10%, TTS_ENABLED=True, TTS_TIMEOUT_SECONDS=30.0, TTS_CONCURRENCY=1, TTS_START_STAGGER_SECONDS=0.3, TTS_RETRIES=1, MAX_SHIFT_SEC=2.0, MAX_VIDEO_SECONDS=1800.0

### Results

| Run | Clip (length / size) | Total s | Extract / analyze / synth s | Final status | Quips (total / audio / no audio) | Spoken s | Audio overlaps | LLM calls (incl. retries) / avg s / max s | Avg prompt / completion tokens | Shifting / Dropping / TTS failed |
|---|---|---|---|---|---|---|---|---|---|---|
| hoco2026_1080p30.mp4 | 326.6 s / 220.8 MB | 1397.1 | 12.1 / 1381.0 / 4.0 | completed | 53 / 53 / 0 | 226.2 | 0 | 33 / 41.1 / 63.4 | 5819 / 120 | 10 / 0 / 0 |

Style: hype.
Phase seconds come from polling every 2 s (about +/-2 s). Total is wall clock from upload accepted to the first poll that saw `completed`/`error`. Overlap = consecutive audio quips where timestamp + duration_sec exceeds the next timestamp by more than 0.05 s.
Window, token and Shifting/Dropping/TTS failed columns are from `backend.log.err`; n/a means the job was not found in the log.

Per-window LLM seconds:

- hoco2026_1080p30.mp4: 63.4s, 41.5s, 40.8s, 42.2s, 40.7s, 40.6s, 39.7s, 38.5s, 40.0s, 39.8s, 39.5s, 39.8s, 40.8s, 41.3s, 40.0s, 40.5s, 40.0s, 41.4s, 40.4s, 40.2s, 41.1s, 40.5s, 39.4s, 40.9s, 41.4s, 41.5s, 39.6s, 40.4s, 40.8s, 40.4s, 41.1s, 40.0s, 39.6s

Long-clip notes (hoco2026.MOV, labeled row above; the row is the 1080p copy, not the 6 GB original):
- Input: the original (4K 120 fps HLG HEVC, 6045 MB) is over the 2 GB cap, so a 1080p 30 fps SDR H.264 copy (220.8 MB) was made first with the same ffmpeg arguments as `transcode.py` (scratch folder outside the repo). That conversion took 537.8 s (about 0.6x real time) and is not in the row's Total; a real upload would add it when the file is accepted. Extract/prepare in the row is only the frame-extraction setup.
- Total 1397 s (23.3 min) for 326.6 s of video, about 4.3x real time; analyze 1381 s, synthesize tail 4 s (speech overlaps analysis).
- 33 windows, 33 LLM calls, no retries, no skipped windows. Seconds per call: 63.4 s for window 1 (cold), then 38.5 to 42.2 s (mean about 40.5 s) with no growth across windows.
- Prompt tokens per call: 5666 to 5864 (flat; the earlier-context summaries are bounded), completion 88 to 149. Highest prompt+completion about 6000 against LLM_CTX 8192.
- 53 quips, all with audio, 0 audio overlaps, 10 shifts, 0 drops, 0 TTS failures (Kokoro, `af_nicole`).
- First playable partial quip: 82.3 s after the job started (window 1 took 63.4 s, plus its TTS).
- Peak backend memory (python.exe, PeakWorkingSet64 read after the run and export, Kokoro loaded): 811 MB. llama-server and ffmpeg are separate processes and were not measured.
- Export with captions (`POST /api/jobs/{id}/export?captions=1`): 68.8 s to render, 127.2 MB output (1080p, 326.6 s).

