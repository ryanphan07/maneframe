import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.config import settings
from app.schemas.events import CommentaryPartial, CommentaryQuip, CommentaryResponse, SkippedRange
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
    partial_cb: Optional[Callable[[CommentaryPartial], None]] = None,
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

    # Quips are keyed by (timestamp, text). Each window's new quips are synthesized in the background while the next
    # window is analyzed; `audio` holds the finished clips.
    audio: Dict[Any, Optional[Dict[str, Any]]] = {}
    queued: set = set()
    tts_tasks: List["asyncio.Task[None]"] = []

    def _key(quip: Dict[str, Any]) -> Any:
        return (quip["timestamp"], quip["text"])

    def _with_audio(quips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged = []
        for quip in quips:
            clip = audio.get(_key(quip))
            payload = dict(quip)
            payload["audio_url"] = clip.get("audio_url") if clip else None
            payload["duration_sec"] = clip.get("duration_sec") if clip and payload["audio_url"] else None
            merged.append(payload)
        return merged

    def _publish() -> None:
        """Hand the finished quips so far to partial_cb: the leading run of queued quips that have their audio result,
        scheduled the same way as the final timeline (scheduling only looks at earlier quips)."""
        if partial_cb is None:
            return
        ready = []
        for quip in finalize_timeline(raw_quips, duration):
            if _key(quip) not in audio:
                break
            ready.append(quip)
        try:
            windows_done = 0  # one task per handled window: count the leading run that has finished
            while windows_done < len(tts_tasks) and tts_tasks[windows_done].done():
                windows_done += 1
            partial_cb(CommentaryPartial(
                quips=[CommentaryQuip(**q) for q in schedule_quips(_with_audio(ready), duration)],
                windows_done=windows_done,
                total_windows=total_windows,
                ready_until=round(windows[windows_done - 1][1], 2) if windows_done else 0.0,
            ))
        except Exception as e:
            logger.warning("Partial result callback failed: %s", e)

    async def _synthesize(batch: List[Dict[str, Any]]) -> None:
        for quip, clip in zip(batch, await asyncio.gather(*(_safe_generate_audio(q) for q in batch))):
            audio[_key(quip)] = clip

    def _queue_new_quips() -> None:
        batch = [q for q in finalize_timeline(raw_quips, duration) if _key(q) not in queued]
        queued.update(_key(q) for q in batch)
        task = asyncio.create_task(_synthesize(batch))
        task.add_done_callback(lambda t: None if t.cancelled() else _publish())
        tts_tasks.append(task)

    analyzing_started = time.monotonic()
    try:
        for i, (start, end) in enumerate(windows):
            avg = (time.monotonic() - analyzing_started) / i if i else None
            await _report("analyzing", 20 + int(45 * i / total_windows), analyzing_message(i, total_windows, avg))
            label = f"window {i + 1}/{total_windows}" + (f" (job {job_id})" if job_id else "")
            try:
                result = await analyze_window_with_retry(video_path, start, end, box, context, label, style)
            except Exception as exc:
                if is_unreachable(exc):
                    logger.error("%s: the model server is unreachable; failing the job instead of skipping", label)
                    raise
                if isinstance(exc, video_processor_service.UnusableVideo):
                    logger.error("%s: %s; failing the job instead of skipping", label, exc)
                    raise
                logger.exception("%s (%.1fs-%.1fs) failed, skipping it", label, start, end)
                skipped.append(SkippedRange(start=round(start, 2), end=round(end, 2)))
                consecutive_skips += 1
                if consecutive_skips >= MAX_CONSECUTIVE_SKIPS or len(skipped) * 2 > total_windows:
                    logger.error("Giving up after skipping %d of %d windows (%d in a row)", len(skipped), total_windows, consecutive_skips)
                    raise
                _queue_new_quips()
                continue
            consecutive_skips = 0
            raw_quips.extend(result.quips)
            context.update(result)
            _queue_new_quips()

        if skipped:
            logger.warning("Skipped %d of %d windows: %s", len(skipped), total_windows, [(r.start, r.end) for r in skipped])

        normalized_quips = finalize_timeline(raw_quips, duration)
        if not normalized_quips:
            raise llm_service.LLMOutputError("The model produced no usable commentary")
        logger.info("Timeline: %d quips from %d windows", len(normalized_quips), total_windows)

        await _report("synthesizing", 70)
        await asyncio.gather(*tts_tasks)
        # A quip that was kept early can be dropped by the final timeline (or a late one added) only when a window
        # returned timestamps outside its own range. Reconcile so the result equals the all-at-end flow.
        final_keys = {_key(q) for q in normalized_quips}
        for key in [k for k in audio if k not in final_keys]:
            clip = audio.pop(key)
            if clip and clip.get("audio_url"):
                tts_service.remove_clip_file(video_id, clip["audio_url"])
        late = [q for q in normalized_quips if _key(q) not in audio]
        for quip, clip in zip(late, await asyncio.gather(*(_safe_generate_audio(q) for q in late))):
            audio[_key(quip)] = clip
    finally:
        pending = [t for t in tts_tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    await _report("synthesizing", 90)

    with_audio = _with_audio(normalized_quips)
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
