# Large uploads: proposal (session 34, no code)

## How big is a phone performance?
Measured on `hoco2026.MOV` (iPhone, 3840x2160, 119.94 fps, HEVC Main 10 HLG HDR, 147 Mbps video, AAC plus a spatial-audio track): 326.6 s = 6 045 MB, about 1.1 GB per minute.

| Length at that setting | Size |
|---|---|
| 1 min | 1.1 GB |
| 2 min | 2.2 GB (already over the cap) |
| 5 min | 5.5 GB |
| 10 min | 11 GB |

This is the 4K 120 fps worst case. Typical iPhone settings (estimates from the usual bitrates, not measured here): 4K 60 fps HEVC is roughly 0.4 GB per minute (a 5 to 10 min performance is 2 to 4 GB), 4K 30 fps roughly 0.2 to 0.3 GB per minute (1 to 3 GB), 1080p 30 fps well under 0.1 GB per minute (under 1 GB). So most 5 to 10 min clips at 4K land at or over the 2 GB cap.

## What happens today
- `POST /api/upload` receives the whole multipart body before the size check: Starlette spools the file to a temp file, then `file.size` is compared with `MAX_UPLOAD_MB` (`video_processor.py:46`). A 6 GB file is fully sent and then rejected with a 400, so the user waits for the whole transfer before seeing the message.
- An accepted file is copied a second time (`main.py` `_save`), so a 2 GB clip briefly needs about 4 GB of free disk.
- Length is checked later (`MAX_VIDEO_SECONDS`, 1800 s). Processing is slow regardless of upload size: the 40 s cut took 7.5 min end to end on a slow-GPU day, so a 5 min clip would be an hour or more (estimate).

## Options
1. **Clear early message (smallest).** The browser knows `File.size` before sending: show "This file is 6.0 GB; the limit is 2 GB. Trim it or export it at 1080p." The page needs the limit from the server (one read-only field on an existing GET, for example `/api/styles`, or a new `GET /api/limits`). Under a day, with a Vitest test. Does not make large clips work.
2. **Client-side trim.** Cut a segment in the browser and upload only that. Cutting without re-encoding needs container parsing; ffmpeg.wasm cannot hold a multi-GB file (wasm memory limit around 2 GB). Poor fit.
3. **Higher cap with a streaming upload.** Replace the multipart parse with a raw body stream (`PUT` reading `request.stream()` straight into the final file, counting bytes and aborting at the cap; the browser sends `fetch(url, {method: "PUT", body: file})`, which streams from disk). One write instead of two, early rejection, and the cap becomes a plain setting. This changes the upload route shape (multipart to raw PUT), so it needs sign-off; keep the multipart route for tests and scripts. Progress from XHR `upload.onprogress`. About 1 to 2 days with tests. After upload the clip is converted to the 1080p 30 fps copy (about 25 MB per minute) and the original is replaced, so disk use settles quickly.
4. **Local-path ingest.** The app is local-first: `POST` a path the user typed, read in place with no copy. Zero transfer, but a browser cannot give a path (no native picker), and it does not carry over to a hosted phase 3.

## Recommendation
Do option 1 now. Do option 3 if teams really upload 5 to 10 min 4K clips, with `MAX_UPLOAD_MB` raised to about 12 000 and `MAX_VIDEO_SECONDS` kept. Skip option 2. Treat option 4 as a developer convenience.

Not measured: a full 5 min clip through the pipeline.
