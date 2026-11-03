from pydantic import BaseModel
from typing import List, Optional

class EventTimeline(BaseModel):
    events: List[dict]

class CommentaryQuip(BaseModel):
    timestamp: float
    text: str
    audio_url: Optional[str] = None
    duration_sec: Optional[float] = None

class SkippedRange(BaseModel):
    start: float
    end: float

class CommentaryResponse(BaseModel):
    quips: List[CommentaryQuip]
    skipped: List[SkippedRange] = []
    total_windows: int = 0
    video_url: Optional[str] = None
    audio_available: bool = False
    style: Optional[str] = None


class CommentaryPartial(BaseModel):
    """Quips finished so far while a job is still running. Never a final result."""
    quips: List[CommentaryQuip]
    windows_done: int = 0
    total_windows: int = 0
    ready_until: float = 0.0  # end time (s) of the last window handled
