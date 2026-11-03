import asyncio
import logging
import mimetypes
import os
import re
import shutil
from typing import NamedTuple, Optional

from app.config import settings
from app.services.export import ExportError, forget_job_locks
from app.services.commentary import process_video_commentary
from app.services.llm import DEFAULT_STYLE, LLMOutputError
from app.services.jobs import job_store
from app.services.transcode import normalize_video
from app.services.video_processor import get_video_duration, is_video_corrupt

logger = logging.getLogger("maneframe.pipeline")


GENERIC_FAILURE = "Commentary generation failed"


class VideoRejected(ValueError):
    """The upload cannot be used; the message is safe to show to the user."""


def friendly_error(exc: BaseException) -> str:
    """A safe, user-facing reason for a failed job. Details stay in the server log."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        openai = None
    if openai is not None and isinstance(exc, openai.APIStatusError):
        logger.warning("LLM server returned HTTP %s", exc.status_code)
        return "The AI model rejected the request. Check the llama-server window for details."
    if openai is not None and isinstance(exc, openai.APIConnectionError):
        return "The AI model is not reachable or timed out. Is llama-server running?"
    if isinstance(exc, ConnectionError):
        return "The AI model is not reachable or timed out. Is llama-server running?"
    if isinstance(exc, LLMOutputError):
        return "The AI model returned unusable output. Try again."
    if isinstance(exc, VideoRejected):
        return str(exc)
    if isinstance(exc, ValueError):
        return "The video could not be processed."
    return GENERIC_FAILURE


def cleanup_job_files(job_id: str, video_path: str) -> None:
    """Delete the job's video, frames dir, audio dir and rendered exports. Never raises."""
    if os.path.exists(video_path):
        try:
            os.remove(video_path)
        except OSError:
            logger.exception("Could not delete video for job %s", job_id)
    for sub in ("frames", "audio", "exports"):
        shutil.rmtree(os.path.join(settings.UPLOAD_DIR, sub, job_id), ignore_errors=True)
    forget_job_locks(job_id)


def purge_expired_jobs() -> int:
    """Forget jobs past their TTL and delete their files. Call this instead of job_store.purge_expired alone."""
    paths = job_store.video_paths()
    purged = job_store.purge_expired()
    for job_id in purged:
        cleanup_job_files(job_id, paths.get(job_id, ""))
    return len(purged)


_VIDEO_NAME = re.compile(r"[0-9a-f]{32}_.+")
_AUDIO_DIR_NAME = re.compile(r"[0-9a-f]{32}")


def sweep_uploads() -> int:
    """Delete leftover files this app created (`<job id>_<name>` videos, `<job id>` audio and export folders). Call at startup, when the in-memory job store is empty."""
    removed = 0
    for sub, pattern, want_dir in (("videos", _VIDEO_NAME, False), ("audio", _AUDIO_DIR_NAME, True), ("exports", _AUDIO_DIR_NAME, True)):
        folder = os.path.join(settings.UPLOAD_DIR, sub)
        if not os.path.isdir(folder):
            continue
        for entry in os.scandir(folder):
            if not pattern.fullmatch(entry.name):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir != want_dir:
                    continue
                if is_dir:
                    shutil.rmtree(entry.path)
                else:
                    os.remove(entry.path)
                removed += 1
            except OSError:
                logger.warning("Could not delete leftover %s", entry.path)
    if removed:
        logger.info("Startup sweep removed %d leftover upload entries", removed)
    return removed


class PreparedVideo(NamedTuple):
    duration: float
    media_type: str  # of the file at video_path after conversion


async def prepare_video(video_path: str, progress_cb) -> PreparedVideo:
    """First job step: make the upload readable (upright H.264 when needed), then check it.
    Raises VideoRejected for a corrupt, unconvertible or too long video."""
    await progress_cb("extracting", 2, "Converting video")
    try:
        readable = not await asyncio.to_thread(is_video_corrupt, video_path)
        converted = await normalize_video(video_path, readable)
    except ExportError:
        raise VideoRejected("The uploaded video could not be converted to a readable format.")
    if await asyncio.to_thread(is_video_corrupt, video_path):
        raise VideoRejected("The uploaded video file is corrupt.")
    try:
        duration = await asyncio.to_thread(get_video_duration, video_path)
    except ValueError as e:
        raise VideoRejected(str(e))
    if duration > settings.MAX_VIDEO_SECONDS:
        raise VideoRejected(f"Video is too long. Maximum length is {settings.MAX_VIDEO_SECONDS:g} seconds.")
    media_type = "video/mp4" if converted else mimetypes.guess_type(video_path)[0] or "application/octet-stream"
    return PreparedVideo(duration, media_type)


async def run_pipeline(job_id: str, video_path: str, style: str = DEFAULT_STYLE) -> None:
    task = asyncio.current_task()
    if task is not None:
        job_store.register_task(job_id, task)
    async def progress_cb(status: str, progress: int, message: Optional[str] = None) -> None:
        try:
            job_store.update(job_id, status, progress, message)
        except Exception:
            logger.warning("Progress update failed for job %s", job_id, exc_info=True)

    try:
        prepared = await prepare_video(video_path, progress_cb)
        job_store.set_video(job_id, prepared.duration, prepared.media_type)
        result = await process_video_commentary(video_path, job_id=job_id, progress_cb=progress_cb, style=style)
        job_store.complete(job_id, result)  # the source video stays until the job expires, for mp4 export
    except asyncio.CancelledError:
        logger.warning("Pipeline for job %s was cancelled", job_id)
        cleanup_job_files(job_id, video_path)
        raise
    except Exception as exc:
        logger.exception("Commentary generation failed for job %s", job_id)
        try:
            job_store.fail(job_id, friendly_error(exc))
        finally:
            cleanup_job_files(job_id, video_path)
