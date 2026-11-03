"""Make phone clips readable: HEVC and other codecs OpenCV's bundled decoder may not handle, and clips that carry
rotation metadata, are transcoded in place to upright H.264 before anything decodes frames."""

import asyncio
import logging
import os
import re
import subprocess
from typing import Awaitable, Callable, Optional, Tuple

from app.config import settings
from app.services import export as export_service
from app.services.export import ExportError

logger = logging.getLogger("maneframe.transcode")

OPENCV_SAFE_CODECS = {"h264", "mpeg4", "mjpeg", "mpeg2video", "vp8"}
TRANSCODE_CRF = 20
ProgressCallback = Callable[[Optional[int]], Awaitable[None]]
PROBE_TIMEOUT_SECONDS = 60
PROGRESS_POLL_SECONDS = 2.0


def parse_probe(stderr: str) -> Tuple[Optional[str], int]:
    """(video codec, display rotation in degrees, 0 when upright) from the text `ffmpeg -i` prints."""
    codec_match = re.search(r"Stream #\d+:\d+.*?: Video: (\w+)", stderr)
    rotation_match = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", stderr)
    rotation = round(float(rotation_match.group(1))) % 360 if rotation_match else 0
    return (codec_match.group(1).lower() if codec_match else None), rotation


def parse_duration(stderr: str) -> Optional[float]:
    """Container duration in seconds from the `Duration: 00:00:40.04` line `ffmpeg -i` prints, if it has one."""
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3)) if match else None


def parse_progress_seconds(text: str) -> Optional[float]:
    """Seconds of output written so far, from the last `out_time_us=` line of an ffmpeg -progress file."""
    values = re.findall(r"^out_time_us=(\d+)\s*$", text, re.MULTILINE)
    return int(values[-1]) / 1e6 if values else None


def needs_transcode(codec: Optional[str], rotation: int, opencv_can_read: bool) -> bool:
    return rotation != 0 or codec not in OPENCV_SAFE_CODECS or not opencv_can_read


async def _watch_progress(path: str, duration: Optional[float], on_progress: ProgressCallback) -> None:
    """Poll ffmpeg's -progress file and report each time the output advanced (percent is None without a duration).
    A hung ffmpeg stops advancing, so it stops counting as job activity and JOB_STALE_SECONDS can still catch it."""
    last = -1.0
    while True:
        await asyncio.sleep(PROGRESS_POLL_SECONDS)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                done = parse_progress_seconds(fh.read())
        except OSError:
            continue
        if done is None or done <= last:
            continue
        last = done
        percent = max(0, min(99, int(done * 100 / duration))) if duration else None
        try:
            await on_progress(percent)
        except Exception:
            logger.warning("Transcode progress callback failed", exc_info=True)


async def normalize_video(video_path: str, opencv_can_read: bool, on_progress: Optional[ProgressCallback] = None) -> bool:
    """Replace `video_path` with an upright H.264 copy when it needs one. Returns True if it was transcoded.

    `on_progress(percent or None)` is awaited while ffmpeg is making progress. The whole conversion is limited by
    TRANSCODE_TIMEOUT_SECONDS. Does nothing when no ffmpeg is available (the caller's own readability check then
    decides). Raises ExportError when the transcode fails, so the caller can report that the clip cannot be read."""
    try:
        ffmpeg = export_service.resolve_ffmpeg()
    except ExportError:
        return False
    folder = os.path.dirname(os.path.abspath(video_path))
    _, probe = await export_service._run([ffmpeg, "-hide_banner", "-nostdin", "-i", video_path], folder, PROBE_TIMEOUT_SECONDS)
    codec, rotation = parse_probe(probe)
    if codec is None:
        return False  # not a video ffmpeg understands: the caller's readability check reports it
    if not needs_transcode(codec, rotation, opencv_can_read):
        return False
    logger.info("Transcoding %s (codec=%s, rotation=%s) to upright H.264", os.path.basename(video_path), codec, rotation)
    name = os.path.basename(video_path)
    partial = f"{name}.transcode.mp4"
    progress_file = f"{name}.transcode.progress"
    args = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", progress_file, "-stats_period", "1", "-i", name,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-crf", str(TRANSCODE_CRF), "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4", partial,
    ]
    watcher = None
    if on_progress is not None:
        watcher = asyncio.create_task(_watch_progress(os.path.join(folder, progress_file), parse_duration(probe), on_progress))
    try:
        try:
            code, stderr = await export_service._run(args, folder, settings.TRANSCODE_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, subprocess.TimeoutExpired):
            logger.error("ffmpeg transcode timed out after %ss", settings.TRANSCODE_TIMEOUT_SECONDS)
            raise ExportError("The video conversion took too long.")
        if code != 0 or not os.path.isfile(os.path.join(folder, partial)):
            logger.error("ffmpeg transcode exited with %s: %s", code, stderr[-export_service.STDERR_TAIL:])
            raise ExportError("The video could not be converted.")
        os.replace(os.path.join(folder, partial), video_path)
    finally:
        if watcher is not None:
            watcher.cancel()
        for leftover in (partial, progress_file):
            try:
                os.remove(os.path.join(folder, leftover))
            except OSError:
                pass
    return True
