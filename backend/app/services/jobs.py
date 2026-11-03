"""
Job management for video commentary processing.
Uses a single event loop, so no threading locks are required.
"""

import asyncio
import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, Field

from app.config import settings
from app.schemas.events import CommentaryPartial, CommentaryResponse

logger = logging.getLogger("maneframe.jobs")

PHASES = ("uploading", "extracting", "analyzing", "synthesizing", "completed", "error")
TERMINAL = {"completed", "error"}


class JobNotFoundError(KeyError):
    pass


class Job(BaseModel):
    job_id: str
    video_path: str
    style: str = "hype"
    status: str = "uploading"
    progress: int = 0
    message: Optional[str] = None
    result: Optional[CommentaryResponse] = None
    partial: Optional[CommentaryPartial] = None  # quips so far; dropped when the job ends
    duration: Optional[float] = None  # set with video_media_type once the video is prepared
    video_media_type: Optional[str] = None  # None until the converted copy at video_path is ready to serve
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


def snapshot(job: Job) -> dict:
    data = {"status": job.status, "progress": job.progress}
    if job.message is not None:
        data["message"] = job.message
    return data


class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def register_task(self, job_id: str, task: asyncio.Task) -> None:
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    def create(self, video_path: str, job_id: Optional[str] = None, style: str = "hype") -> Job:
        self.purge_expired()
        if job_id is None:
            job_id = str(uuid.uuid4())
        if job_id in self._jobs:
            raise ValueError(f"Job {job_id} already exists")
        job = Job(job_id=job_id, video_path=video_path, style=style)
        self._jobs[job_id] = job
        logger.info(f"Created job {job_id}")
        return job

    def set_video(self, job_id: str, duration: float, media_type: str) -> None:
        job = self.get(job_id)
        job.duration = duration
        job.video_media_type = media_type

    def set_partial(self, job_id: str, partial: CommentaryPartial) -> None:
        job = self.get(job_id)
        if job.status not in TERMINAL:
            job.partial = partial

    def get(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise JobNotFoundError(job_id)
        return self._jobs[job_id]

    def update(self, job_id: str, status: str, progress: int, message: Optional[str] = None) -> Job:
        if status not in PHASES:
            raise ValueError(f"Invalid status: {status}")
        
        job = self.get(job_id)
        
        if job.status in TERMINAL:
            logger.debug(f"Job {job_id} is already terminal, ignoring update")
            return job

        # Clamp progress
        progress = max(0, min(100, progress))
        
        # Monotonicity for non-error updates
        if status != "error":
            progress = max(job.progress, progress)
            
        job.status = status
        job.progress = progress
        if message is not None:
            job.message = message
        job.updated_at = time.time()
        
        # Publish to subscribers
        if job_id in self._subscribers:
            snap = snapshot(job)
            for q in self._subscribers[job_id]:
                q.put_nowait(snap)
                
        return job

    def complete(self, job_id: str, result: CommentaryResponse):
        job = self.get(job_id)
        if job.status not in TERMINAL:
            job.result = result
            job.partial = None
            message = "Commentary ready" if result.audio_available else "Commentary ready (audio unavailable)"
            if result.skipped:
                message += f", skipped {len(result.skipped)} of {result.total_windows} parts"
            self.update(job_id, "completed", 100, message)

    def fail(self, job_id: str, message: str):
        if not message:
            message = "Processing failed"
        job = self.get(job_id)
        job.partial = None
        if job.status in TERMINAL:
            job.message = message
            job.updated_at = time.time()
            return job
        self.update(job_id, "error", job.progress, message)

    def subscribe(self, job_id: str) -> asyncio.Queue:
        job = self.get(job_id)
        q = asyncio.Queue()
        q.put_nowait(snapshot(job))
        if job_id not in self._subscribers:
            self._subscribers[job_id] = []
        self._subscribers[job_id].append(q)
        return q

    def unsubscribe(self, job_id: str, queue: asyncio.Queue):
        if job_id in self._subscribers:
            try:
                self._subscribers[job_id].remove(queue)
            except ValueError:
                pass

    def has_active(self) -> bool:
        """True if a job is running. A job with no progress update for JOB_STALE_SECONDS is failed first."""
        now = time.time()
        for job in list(self._jobs.values()):
            if job.status not in TERMINAL and now - job.updated_at > settings.JOB_STALE_SECONDS:
                logger.warning("Job %s made no progress for %ds; marking it failed", job.job_id, settings.JOB_STALE_SECONDS)
                self.fail(job.job_id, "Processing timed out")
                task = self._tasks.pop(job.job_id, None)
                if task is not None:
                    task.cancel()
        return any(job.status not in TERMINAL for job in self._jobs.values())

    def video_paths(self) -> dict[str, str]:
        return {job_id: job.video_path for job_id, job in self._jobs.items()}

    def purge_expired(self) -> list[str]:
        now = time.time()
        purged = []
        for job_id, job in list(self._jobs.items()):
            if job.status in TERMINAL and (now - job.updated_at) > settings.JOB_TTL_SECONDS:
                del self._jobs[job_id]
                self._subscribers.pop(job_id, None)
                purged.append(job_id)
        return purged


job_store = JobStore()
