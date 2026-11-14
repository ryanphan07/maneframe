# Design decisions

Design decisions and the reasoning behind them. Add new entries at the bottom with a date.

## 2026-10-28

### D1. Long clips are a goal (10+ minutes), not a 60/90 s cap
- Processing clips longer than 10 minutes is a goal. The 60 s limit was a temporary MVP guard, not a product decision.
- Consequences, measured on the 9B model: about 54 s per LLM call (docs/BENCHMARKS.md; a 40 s clip took 299 s, about 7x real time), so a 10 minute clip would be roughly 70 minutes: extrapolated, untested. The 35B-A3B model will differ; measure it.
- Windows are independent apart from the two previous quips, so length scales linearly and memory stays flat (frames are sampled per window).
- Limits become settings: `MAX_VIDEO_SECONDS` (default 1800) and `MAX_UPLOAD_MB` (default 2048). They are guards against accidents, not promises of speed.
- Open risks for long jobs (tracked in docs/TASKS.md): one failed window currently fails the whole job after possibly 40 minutes of work (consider per-window retry or skipping with a logged gap); no persistence, so a server restart loses the job; progress needs an ETA to be tolerable; the README demo should use a short clip.

### D2. Models: develop on Qwen3.5-9B, use Qwen3.6-35B-A3B for real runs
- Defaults and docs must work with either; nothing may hard-code model behavior. Timing numbers in docs are labeled with the model they were measured on (9B so far).
- Prompt and word/gap calibration must be re-checked on the 35B before declaring quality done.

### D3. TTS provider and fallback
- Today: edge-tts only (unofficial Microsoft endpoint, needs internet, broke once with 403). On any failure the quip is kept as text with no audio and the UI shows a notice. This is honest and already implemented.
- Options considered:
  1. Keep edge-tts only, text-only fallback (current). Zero work; demo depends on internet and an unofficial endpoint.
  2. Add an offline provider (Piper is the usual choice: local, free, decent quality, Windows binaries available) behind `TTS_PROVIDER`. Moderate work; makes the app fully local, which matches "local-first" and removes demo-day risk.
  3. Windows built-in SAPI via pyttsx3: trivial, but robotic and weak for a comedy demo.
- Recommendation: do option 1 now, finish and ship the MVP, add option 2 after v0.1.0. Revisit if edge-tts breaks again. Nothing here changes the job API: `audio_url` stays nullable.
- Status: deferred until after v0.1.0.

### D4. Long-job strategy
A 10+ minute clip is one job of about 60 windows. Today one failed window fails everything and nothing is shown until the end.
- A. Keep all-or-nothing (current). Simple and honest, but a late failure wastes up to 45 minutes.
- B. Skip a failed window after one retry, log it, and tell the user which time ranges have no commentary ("skipped 2 of 60 parts"). Small change; still honest because the gap is reported, not faked. Changes the earlier "a failed window ends the job" rule.
- C. Progressive results: synthesize each window as it finishes and let the UI show and play commentary while the rest processes. Best UX for long clips; changes the job result API (partial results) and the player. Larger change.
- Recommendation: B now, C after v0.1.0.

## 2026-10-28 (later)

### D4 decision: A
- Keep all-or-nothing for v0.1.0. Revisit B (skip a failed window after one retry, report the gap) and C (progressive results) after release.

### D5. License
- MIT, copyright "Ryan Phan 2026".

## 2026-10-29

### D4 decision (revised): B
- Supersedes "D4 decision: A". The project goal is now real users (see docs/TASKS.md), and losing up to 45 minutes of work to one bad window is not acceptable for them.
- Policy: a window that fails is retried once, then skipped and logged. An unusable model reply is already retried once inside `analyze_window`; other failures (transport, timeout) get one more attempt in `commentary.analyze_window_with_retry`; frames that cannot be sampled are retried the same way. Worst case for a hung model is two timeouts per window.
- The result carries `skipped` (list of `{start, end}` seconds) and `total_windows`; the job message reads "Commentary ready, skipped n of m parts" and the player lists the skipped ranges. Nothing is invented for a skipped range, so the honesty rule holds.
- A job where every window fails still ends in `error`, with the category message of the last failure. A job where no window failed but no quips were produced is also still an error.
- Option C (progressive results) stays in phase 2.

### LLM_JSON_SCHEMA fallback
- If the server answers HTTP 400 while `LLM_JSON_SCHEMA` is on (some servers reject `response_format`), that one call is retried once without it and a warning is logged. Other statuses and a second 400 propagate. The setting itself is not changed.


### D4 skip limits (2026-10-29, session 21)
- Skipping must not hide a broken run. The job fails immediately, without retry, when the model server is unreachable (connection refused, DNS, dropped; a timeout is not, since a slow server is retried and skipped like any other failure). It also fails as soon as more than half the windows are skipped, or 3 windows in a row are skipped. The error shown is the category message of the last failure.
- Constants: `MAX_CONSECUTIVE_SKIPS` in `commentary.py`; the half rule is computed from `total_windows`.


## 2026-11-04

### D6. Phase 3 is local only
- Distribution work in phase 3 stays local: no hosted service, no accounts, no uploads to a server we run.

### D7. Default voice stays edge-tts
- The default TTS voice stays edge-tts. The owner prefers it over Piper and finds both robotic; other voice options are still to be explored.

## 2026-11-05

### D8. Workflow: session branches, PRs and CI
- Decision: each Claude Code session works on its own branch and ends with a PR. GitHub Actions runs the backend tests (windows-latest) and the frontend test, lint and build (ubuntu-latest) on every PR and on pushes to main. The owner reviews a bundle (PR, handoff, commits, diff, CI result, local re-run) and merges with a merge commit.
- Alternative considered: commit to local main and let the owner push (the workflow through session 49).
- Why: the planning chat reviewed only the handoff, never the diff, and test results were the session's own report.
- Tradeoff: one push and a CI wait per session, and session branches are visible in the public repo. Merge commits, not squash, keep the one-logical-change-per-commit history.

## 2026-11-05 (later)

### D9. Automatic framing for the 9:16 export
- Decision: the 9:16 export frames each clip automatically: a fixed-size crop window that follows the motion over time, or, when the action is wider than that window for much of the clip, the whole frame scaled to the 9:16 width with a blurred copy filling above and below. Captions are placed where they do not cover the performers. There is no user framing control.
- Alternative considered: one static crop box per video (the first 9:16 version), or a user-chosen framing option in the export controls.
- Why: the static box cut out performers who spread apart, and teams want a usable post in one click.
- Tradeoff: the automatic choice can be wrong for a clip and there is no manual override yet; tracking adds export time.


## 2026-11-09

### D10. Distribution as a portable Windows zip
- Decision: Maneframe ships as a zip with a private Python and its packages, the prebuilt frontend, the llama.cpp Vulkan llama-server and a Start Maneframe.bat. Unzip and double-click; no Python, Node, git or ffmpeg needed.
- Alternative considered: a setup script run on a git clone; an Inno Setup installer with a PyInstaller-frozen backend.
- Why: lion dance team members are not developers, and a zip has the fewest moving parts that can fail on someone else's machine.
- Tradeoff: Windows SmartScreen warns on first run (no code signing), the zip is large, Windows only, and updating means downloading a new zip.

### D11. Model chosen at first run
- Decision: on first run the launcher detects RAM and GPU memory, recommends Qwen3.6-35B-A3B or Qwen3.5-9B, lets the user override, and downloads the chosen model from Hugging Face into the data folder with a checksum check. Models are never bundled in the zip.
- Alternative considered: always the 35B; always the 9B; bundling a model in the zip.
- Why: team laptops vary; the 35B needs about 16 GB of memory, the 9B runs on less, and keeping models out of the zip keeps it small and avoids redistributing model files.
- Tradeoff: one more first-run step and a 7 to 16 GB download; the recommendation can be wrong; the 9B has not been tuned with the current prompt.

### D12. App data in %LOCALAPPDATA%\Maneframe
- Decision: models, uploads, labels and logs live in %LOCALAPPDATA%\Maneframe (overridable with MANEFRAME_DATA_DIR); the unzipped app folder holds only the program.
- Alternative considered: everything inside the unzipped folder.
- Why: the model survives unzipping a newer version, and the app folder can sit anywhere, including read-only locations.
- Tradeoff: deleting the app folder leaves the data behind, so the README must say where it is and how to remove it.

### D13. Model recommendation uses RAM only
- Decision: the first-run chooser recommends Qwen3.6-35B-A3B when Windows reports at least 15 GB of total RAM, else Qwen3.5-9B. GPU memory is shown on the chooser screen but not used.
- Alternative considered: also recommending the 35B at 16 GB of GPU memory, as specified for s56.
- Why: llama.cpp reports an integrated GPU's shared memory, not dedicated memory (23.8 GB on the development PC with 15.6 GB of RAM), so a GPU rule would recommend a 16.5 GB download to laptops that cannot run it.
- Tradeoff: a PC with a large discrete GPU but less than 15 GB of RAM is offered the 9B; the user can still type 35.

## 2026-11-10

### D14. Missing Visual C++ runtime: detect and explain
- Decision: the zip does not bundle the Visual C++ runtime. When llama-server cannot start because a DLL is missing, the launcher says which runtime to install and gives Microsoft's official installer link.
- Alternative considered: copying the runtime DLLs next to llama-server.exe (app-local deployment).
- Why: no redistribution setup and nothing extra to keep up to date in the build; most Windows PCs already have the runtime.
- Tradeoff: a user without it installs one Microsoft package once before the first run.
- Superseded by D17.

### D15. First releases are unsigned
- Decision: the zip is published without code signing. The README and Read me first.txt explain the Windows warning ("Open File - Security Warning" or SmartScreen) and how to avoid it by unblocking the zip before extracting.
- Alternative considered: buying a code-signing certificate or signing service before the first release.
- Why: no cost before anyone outside the team has used the app.
- Tradeoff: users who download the zip see an "unknown publisher" warning, which some will not get past.

### D16. Reconnect a slow download
- Decision: when a model download averages under 5 MB/s over 30 s, the launcher closes the connection and resumes with a new request (Range), at most 5 times per file, then keeps the connection it has. These reconnects do not count against the 3 error retries.
- Alternative considered: keeping one connection until it fails (s56 behavior).
- Why: on 2026-11-10 one Hugging Face connection ran at 1.8 MB/s for 29 minutes on a 1 Gbps line; the next connection ran at 67 MB/s.
- Tradeoff: on a line that is really slower than 5 MB/s, the launcher reconnects 5 times for nothing (a second or two each).

### D17. Visual C++ runtime is bundled with the zip
- Decision: the zip carries msvcp140, msvcp140_1, vcruntime140 and vcruntime140_1 next to python.exe and llama-server.exe (app-local deployment, copied by `scripts/package.ps1` since s55). Supersedes D14.
- Alternative considered: D14 as written: no runtime in the zip, and a launcher message pointing to Microsoft's installer when a DLL is missing.
- Why: it was already built and tested that way (MC-8), and teammates on a fresh PC get no missing-DLL error.
- Tradeoff: the bundled copies are the build PC's versions and are not updated by Windows Update; each new zip carries whatever the build PC has.

### D18. ffmpeg is downloaded at first run, not bundled
- Decision: the zip contains no ffmpeg executable. On first run the launcher downloads the pinned imageio-ffmpeg wheel (the version in backend/requirements.txt) from PyPI, checks its sha256, extracts the ffmpeg executable into the data folder and passes it to the backend as FFMPEG_PATH. Running from the repo is unchanged.
- Alternative considered: bundling the binary with the GPL v3 text and the corresponding source attached to each Release; or an LGPL ffmpeg build without libx264.
- Why: the imageio-ffmpeg binary is a GPL v3 build; not distributing it removes the source-offer obligation, keeps the exact binary already tested, and makes the zip smaller.
- Tradeoff: one more first-run download that depends on PyPI; if it cannot be downloaded the launcher stops with a message, like a failed model download.

### D19. mutagen stays in the zip
- Decision: mutagen (GPL-2.0-or-later, used to read audio durations) stays in the portable zip. Its licence text is in THIRD_PARTY_NOTICES, its complete source ships unchanged as .py files, and THIRD_PARTY_NOTICES\INDEX.txt says so.
- Alternative considered: replacing it with a permissively licensed duration reader.
- Why: mutagen is pure Python, so the zip already carries its source; Maneframe's own code is MIT (GPL-compatible) and public. No code change or re-test needed.
- Tradeoff: the zip as a whole is distributed on GPL-compatible terms; a closed-source build would have to replace mutagen.

## 2026-11-13

### D20. llama-server fits the model to the GPUs itself
- Decision: the launcher no longer passes `-ngl 99`; llama-server's automatic fit decides how many layers go to each GPU and keeps the rest in RAM. If llama-server still exits while loading, the launcher reads the end of llama-server.log and, for an out-of-memory error, tells the user to choose the smaller model with Change model.bat.
- Alternative considered: keeping `-ngl 99` and restarting without it only after a failed load.
- Why: in MC-9 (2026-11-13) the 35B failed to load with `-ngl 99` on a laptop with 32 GB of RAM, an Intel iGPU and an 8 GB RTX 4060 (Vulkan out of device memory); the same files loaded with the automatic fit.
- Tradeoff: llama.cpp chooses the split, so speed can differ from `-ngl 99`; measured on the development PC in s59.
