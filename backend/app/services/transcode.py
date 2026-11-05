"""Make phone clips readable and a sane size: HEVC and other codecs OpenCV's bundled decoder may not handle, clips that
carry rotation metadata, HDR clips, and clips whose long side exceeds TRANSCODE_MAX_SIDE (4K H.264 included) are
transcoded in place to upright SDR H.264 before anything decodes frames."""

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


def parse_fps(stderr: str) -> Optional[float]:
    """Frame rate of the first video stream from the `119.94 fps` in the text `ffmpeg -i` prints."""
    match = re.search(r"Stream #\d+:\d+.*?: Video:.*?, (\d+(?:\.\d+)?) fps", stderr)
    return float(match.group(1)) if match else None


def is_hdr(stderr: str) -> bool:
    """True for HLG (arib-std-b67) or PQ (smpte2084) video, which phones record in HDR mode."""
    return re.search(r"Stream #\d+:\d+.*?: Video:.*?(arib-std-b67|smpte2084)", stderr) is not None


def parse_size(stderr: str) -> Optional[Tuple[int, int]]:
    """(width, height) of the first video stream from the `3840x2160` in the text `ffmpeg -i` prints."""
    match = re.search(r"Stream #\d+:\d+.*?: Video:.*?\b(\d{2,5})x(\d{2,5})\b", stderr)
    return (int(match.group(1)), int(match.group(2))) if match else None


def build_video_filter(fps: Optional[float], hdr: bool) -> str:
    """The -vf chain: frame rate capped at TRANSCODE_MAX_FPS, long side capped at TRANSCODE_MAX_SIDE (even sizes),
    and HDR tone-mapped to SDR BT.709, so a 4K 120 fps HDR phone clip becomes a 1080p 30 fps SDR clip that converts in
    reasonable time and does not look washed out. Commentary and the exported mp4 both use this converted copy."""
    side = int(settings.TRANSCODE_MAX_SIDE)
    chain = []
    if fps is not None and fps > settings.TRANSCODE_MAX_FPS + 0.5:
        chain.append(f"fps={_fmt(settings.TRANSCODE_MAX_FPS)}")
    chain.append(f"scale=w='if(gt(iw,ih),min(iw,{side}),-2)':h='if(gt(iw,ih),-2,min(ih,{side}))'")
    if hdr:
        chain.append(
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=tv"
        )
    chain.append("format=yuv420p")
    return ",".join(chain)


def _fmt(value: float) -> str:
    return f"{value:g}"


def parse_duration(stderr: str) -> Optional[float]:
    """Container duration in seconds from the `Duration: 00:00:40.04` line `ffmpeg -i` prints, if it has one."""
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3)) if match else None


def parse_progress_seconds(text: str) -> Optional[float]:
    """Seconds of output written so far, from the last `out_time_us=` line of an ffmpeg -progress file."""
    values = re.findall(r"^out_time_us=(\d+)\s*$", text, re.MULTILINE)
    return int(values[-1]) / 1e6 if values else None


def needs_transcode(codec: Optional[str], rotation: int, opencv_can_read: bool,
                    size: Optional[Tuple[int, int]] = None, hdr: bool = False) -> bool:
    """True for rotation metadata, a codec OpenCV may not read, an unreadable file, HDR (HLG/PQ), or a long side above
    TRANSCODE_MAX_SIDE. Frame rate alone is no trigger: frame sampling seeks, so it barely affects analysis."""
    too_big = size is not None and max(size) > int(settings.TRANSCODE_MAX_SIDE)
    return rotation != 0 or codec not in OPENCV_SAFE_CODECS or not opencv_can_read or hdr or too_big


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
    hdr = is_hdr(probe)
    size = parse_size(probe)
    if not needs_transcode(codec, rotation, opencv_can_read, size, hdr):
        return False
    logger.info("Transcoding %s (codec=%s, size=%s, rotation=%s, hdr=%s) to upright SDR H.264", os.path.basename(video_path), codec, size, rotation, hdr)
    name = os.path.basename(video_path)
    partial = f"{name}.transcode.mp4"
    progress_file = f"{name}.transcode.progress"
    args = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", progress_file, "-stats_period", "1", "-i", name,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", build_video_filter(parse_fps(probe), hdr),
        "-c:v", "libx264", "-crf", str(TRANSCODE_CRF), "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
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
