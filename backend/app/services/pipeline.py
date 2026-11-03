import asyncio
import logging
import os
import re
import shutil
from typing import Optional

from app.config import settings
from app.services.export import forget_job_locks
from app.services.commentary import process_video_commentary
from app.services.llm import DEFAULT_STYLE, LLMOutputError
from app.services.jobs import job_store

logger = logging.getLogger("maneframe.pipeline")


GENERIC_FAILURE = "Commentary generation failed"


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
