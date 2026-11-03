import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.config import settings
from app.schemas.events import CommentaryQuip, CommentaryResponse, SkippedRange
from app.services import llm as llm_service
from app.services import tts as tts_service
from app.services import video_processor as video_processor_service

logger = logging.getLogger("maneframe.commentary")


AUDIO_GAP_SEC = 0.3
MAX_CONSECUTIVE_SKIPS = 3
CONTEXT_WINDOWS = 2  # how many earlier windows' "seen" summaries and recent quips go into the next window's message


@dataclass
class Continuity:
    """What the next window's message carries over from earlier windows. A skipped window leaves it unchanged."""
    quips: List[str] = field(default_factory=list)
    seen: List[str] = field(default_factory=list)

    def update(self, result: "llm_service.WindowResult") -> None:
        if result.quips:
            self.quips = [q["text"] for q in result.quips[-CONTEXT_WINDOWS:]]
        if result.seen:
            self.seen = (self.seen + [result.seen])[-CONTEXT_WINDOWS:]


def is_unreachable(exc: BaseException) -> bool:
    """True when the model server cannot be reached at all (refused, DNS, dropped). A timeout is not: the server is
    there but slow, so that window is retried and skipped like any other failure."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        openai = None
    if openai is not None and isinstance(exc, openai.APIConnectionError):
        return not isinstance(exc, openai.APITimeoutError)
    return isinstance(exc, ConnectionError)


def finalize_timeline(quips: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """Sort, clamp to the video, enforce the minimum gap, drop empty text. Text is never cut: overlong lines are
    handled by schedule_quips after their real audio duration is known."""
    clamped = [
        {"timestamp": round(min(max(q["timestamp"], 0.0), duration), 2), "text": " ".join(q["text"].split())}
        for q in sorted(quips, key=lambda q: q["timestamp"])
    ]
    kept: List[Dict[str, Any]] = []
    for quip in clamped:
        if not quip["text"]:
            continue
        if kept and quip["timestamp"] - kept[-1]["timestamp"] < settings.MIN_QUIP_GAP_SEC:
            continue
        kept.append(quip)
    return kept


def analyzing_message(index: int, total: int, avg_window_sec: Optional[float]) -> str:
    """'Analyzing part i of n', plus a time estimate once one window has been timed."""
    message = f"Analyzing part {index + 1} of {total}"
    if avg_window_sec:
        minutes = avg_window_sec * (total - index) / 60
        message += ", under a minute left" if minutes < 1 else f", about {round(minutes)} min left"
    return message


def schedule_quips(quips: List[Dict[str, Any]], video_duration: Optional[float] = None) -> List[Dict[str, Any]]:
    """Keep spoken quips from overlapping: shift late by up to MAX_SHIFT_SEC, else drop. Quips without audio never block."""
    result: List[Dict[str, Any]] = []
    audio_end: Optional[float] = None
    for quip in sorted(quips, key=lambda q: q["timestamp"]):
        quip = dict(quip)
        if quip.get("audio_url") and quip.get("duration_sec"):
            start = quip["timestamp"]
            if audio_end is not None and start < audio_end + AUDIO_GAP_SEC:
                new_start = audio_end + AUDIO_GAP_SEC
                shift = new_start - start
                if shift > settings.MAX_SHIFT_SEC or (video_duration is not None and new_start >= video_duration):
                    logger.warning("Dropping quip at t=%.2f: would need a %.2fs shift (max %.2fs) or start after the video ends", start, shift, settings.MAX_SHIFT_SEC)
                    continue
                logger.info("Shifting quip from t=%.2f to t=%.2f to avoid audio overlap", start, new_start)
                quip["timestamp"] = start = round(new_start, 2)
            audio_end = start + quip["duration_sec"]
        result.append(quip)
    return result


async def analyze_window_with_retry(
    video_path: str,
    start: float,
    end: float,
    box: Any,
    context: Continuity,
    label: str,
    style: str = llm_service.DEFAULT_STYLE,
) -> "llm_service.WindowResult":
    """Sample and analyze one window. A reply that was not usable JSON was already retried inside analyze_window;
    any other failure (timeout, server error) gets one more attempt here. An unreachable server is not retried."""
    for attempt in range(2):
        frames = await asyncio.to_thread(
            video_processor_service.sample_window_frames,
            video_path, start, end, settings.FRAMES_PER_WINDOW, settings.FRAME_WIDTH, box,
        )
        if not frames:
            raise ValueError(f"No frames could be extracted for {label}")
        try:
            return await llm_service.analyze_window(
                frames, start, end, context.quips, label=label, earlier_seen=context.seen, style=style,
            )
        except llm_service.LLMOutputError:
            raise
        except Exception as exc:
            if attempt or is_unreachable(exc):
                raise
            logger.warning("%s failed (%s: %s); retrying once", label, type(exc).__name__, exc)
    raise AssertionError("unreachable")


async def process_video_commentary(
    video_path: str,
    job_id: Optional[str] = None,
    progress_cb: Optional[Callable[[str, int, Optional[str]], Awaitable[None]]] = None,
    style: str = llm_service.DEFAULT_STYLE,
) -> CommentaryResponse:
    video_id = job_id or "default"
    started = time.monotonic()
    logger.info("Starting commentary generation for video_id: %s", video_id)

    async def _report(status: str, progress: int, message: Optional[str] = None) -> None:
        if progress_cb is None:
            return
        try:
            await progress_cb(status, progress, message)
        except Exception as e:
            logger.warning("Progress callback failed: %s", e)

    await _report("extracting", 10, "Finding the action")
    duration = await asyncio.to_thread(video_processor_service.get_video_duration, video_path)
    box = await asyncio.to_thread(video_processor_service.compute_crop_box, video_path, duration)
    windows = video_processor_service.plan_windows(duration, settings.WINDOW_SECONDS)
    if not windows:
        raise ValueError("No frames could be extracted from video")

    raw_quips: List[Dict[str, Any]] = []
    context = Continuity()
    skipped: List[SkippedRange] = []
    consecutive_skips = 0
    total_windows = len(windows)
    analyzing_started = time.monotonic()
    for i, (start, end) in enumerate(windows):
        avg = (time.monotonic() - analyzing_started) / i if i else None
        await _report("analyzing", 20 + int(45 * i / total_windows), analyzing_message(i, total_windows, avg))
        label = f"window {i + 1}/{total_windows}"
        try:
            result = await analyze_window_with_retry(video_path, start, end, box, context, label, style)
        except Exception as exc:
            if is_unreachable(exc):
                logger.error("%s: the model server is unreachable; failing the job instead of skipping", label)
                raise
            logger.exception("%s (%.1fs-%.1fs) failed, skipping it", label, start, end)
            skipped.append(SkippedRange(start=round(start, 2), end=round(end, 2)))
            consecutive_skips += 1
            if consecutive_skips >= MAX_CONSECUTIVE_SKIPS or len(skipped) * 2 > total_windows:
                logger.error("Giving up after skipping %d of %d windows (%d in a row)", len(skipped), total_windows, consecutive_skips)
                raise
            continue
        consecutive_skips = 0
        raw_quips.extend(result.quips)
        context.update(result)

    if skipped:
        logger.warning("Skipped %d of %d windows: %s", len(skipped), total_windows, [(r.start, r.end) for r in skipped])

    normalized_quips = finalize_timeline(raw_quips, duration)
    if not normalized_quips:
        raise llm_service.LLMOutputError("The model produced no usable commentary")
    logger.info("Timeline: %d quips from %d windows", len(normalized_quips), total_windows)

    await _report("synthesizing", 70)
    semaphore = asyncio.Semaphore(settings.TTS_CONCURRENCY)

    start_lock = asyncio.Lock()
    last_start = 0.0

    async def _stagger() -> None:
        nonlocal last_start
        async with start_lock:
            wait = last_start + settings.TTS_START_STAGGER_SECONDS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            last_start = time.monotonic()

    async def _safe_generate_audio(quip: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with semaphore:
            await _stagger()
            try:
                return await tts_service.generate_audio_clip(quip, video_id=video_id)
            except Exception as e:
                logger.warning("TTS failed for t=%.2f: %s", quip["timestamp"], e)
                return None

    finished = 0

    async def _counted(quip: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        nonlocal finished
        result = await _safe_generate_audio(quip)
        finished += 1
        await _report("synthesizing", 70 + int(20 * finished / len(normalized_quips)))
        return result

    results = await asyncio.gather(*(_counted(q) for q in normalized_quips))
    await _report("synthesizing", 90)

    with_audio: List[Dict[str, Any]] = []
    for quip, result in zip(normalized_quips, results):
        payload = dict(quip)
        payload["audio_url"] = result.get("audio_url") if result else None
        payload["duration_sec"] = result.get("duration_sec") if result and payload["audio_url"] else None
        with_audio.append(payload)

    scheduled = schedule_quips(with_audio, duration)
    kept_urls = {q["audio_url"] for q in scheduled if q["audio_url"]}
    for q in with_audio:
        if q["audio_url"] and q["audio_url"] not in kept_urls:
            tts_service.remove_clip_file(video_id, q["audio_url"])
    audio_available = any(q["audio_url"] for q in scheduled)
    if not audio_available:
        logger.error("No audio clips available for video_id: %s", video_id)

    logger.info("Pipeline for %s finished in %.1fs", video_id, time.monotonic() - started)
    return CommentaryResponse(
        quips=[CommentaryQuip(**q) for q in scheduled],
        skipped=skipped,
        total_windows=total_windows,
        audio_available=audio_available,
        style=style,
    )
