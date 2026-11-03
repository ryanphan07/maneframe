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
