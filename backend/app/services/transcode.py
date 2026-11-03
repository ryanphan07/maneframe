"""Make phone clips readable: HEVC and other codecs OpenCV's bundled decoder may not handle, and clips that carry
rotation metadata, are transcoded in place to upright H.264 before anything decodes frames."""

import logging
import os
import re
from typing import Optional, Tuple

from app.config import settings
from app.services import export as export_service
from app.services.export import ExportError

logger = logging.getLogger("maneframe.transcode")

OPENCV_SAFE_CODECS = {"h264", "mpeg4", "mjpeg", "mpeg2video", "vp8"}
TRANSCODE_CRF = 20
PROBE_TIMEOUT_SECONDS = 60


def parse_probe(stderr: str) -> Tuple[Optional[str], int]:
    """(video codec, display rotation in degrees, 0 when upright) from the text `ffmpeg -i` prints."""
    codec_match = re.search(r"Stream #\d+:\d+.*?: Video: (\w+)", stderr)
    rotation_match = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", stderr)
    rotation = round(float(rotation_match.group(1))) % 360 if rotation_match else 0
    return (codec_match.group(1).lower() if codec_match else None), rotation


def needs_transcode(codec: Optional[str], rotation: int, opencv_can_read: bool) -> bool:
    return rotation != 0 or codec not in OPENCV_SAFE_CODECS or not opencv_can_read


async def normalize_video(video_path: str, opencv_can_read: bool) -> bool:
    """Replace `video_path` with an upright H.264 copy when it needs one. Returns True if it was transcoded.

    Does nothing when no ffmpeg is available (the caller's own readability check then decides). Raises ExportError
    when the transcode fails, so the caller can report that the clip cannot be read."""
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
    args = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", name,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-crf", str(TRANSCODE_CRF), "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4", partial,
    ]
    try:
        code, stderr = await export_service._run(args, folder, settings.EXPORT_TIMEOUT_SECONDS)
        if code != 0 or not os.path.isfile(os.path.join(folder, partial)):
            logger.error("ffmpeg transcode exited with %s: %s", code, stderr[-export_service.STDERR_TAIL:])
            raise ExportError("The video could not be converted.")
        os.replace(os.path.join(folder, partial), video_path)
    finally:
        try:
            os.remove(os.path.join(folder, partial))
        except OSError:
            pass
    return True
