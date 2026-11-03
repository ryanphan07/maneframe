import asyncio
import hashlib
import logging
import os
import re
import threading
import wave
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger("maneframe.tts")

try:
    from edge_tts import Communicate
    from edge_tts.exceptions import NoAudioReceived, WebSocketError
except ImportError:  # pragma: no cover
    Communicate = None
    NoAudioReceived = WebSocketError = None

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

try:
    from mutagen.mp3 import MP3
except ImportError:  # pragma: no cover
    MP3 = None

try:
    from piper import PiperVoice
    from piper.config import SynthesisConfig
except ImportError:  # piper-tts is optional (TTS_ENGINE=piper)
    PiperVoice = SynthesisConfig = None

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MIN_AUDIO_BYTES = 200
DURATION_RECHECK_DELAY_SECONDS = 0.2


def measure_duration(path: str) -> Optional[float]:
    """Return the mp3 or wav duration in seconds, or None if it cannot be read."""
    try:
        if path.lower().endswith(".wav"):
            with wave.open(path, "rb") as w:
                length = w.getnframes() / float(w.getframerate())
        elif MP3 is None:
            return None
        else:
            length = float(MP3(path).info.length)
    except Exception:
        logger.warning("Could not measure duration of %s", path, exc_info=True)
        return None
    return length if length > 0 else None


def _is_transient(exc: BaseException) -> bool:
    """Timeouts, dropped connections and empty edge-tts replies are worth one more try; auth failures and bad requests are not."""
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    if NoAudioReceived is not None and isinstance(exc, (NoAudioReceived, WebSocketError)):
        return True
    if aiohttp is not None:
        if isinstance(exc, aiohttp.ClientResponseError):
            return exc.status >= 500
        if isinstance(exc, aiohttp.ClientConnectionError):
            return True
    return False


def _file_size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _log_failure(exc: BaseException, timestamp: float, text: str, path: str) -> None:
    logger.warning(
        "TTS failed for t=%.2f: %s: %s (text_len=%d voice=%s rate=%s file_bytes=%s)",
        timestamp, type(exc).__name__, exc, len(text), settings.TTS_VOICE, settings.TTS_RATE, _file_size(path),
    )


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not delete partial audio file %s", path)


def clip_path(video_id: str, audio_url: Optional[str]) -> Optional[str]:
    """The server-side path of the mp3 or wav behind an audio_url, or None if the id or url is not one this app produced."""
    if not audio_url or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video_id):
        return None
    name = os.path.basename(audio_url.split("?")[0])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(mp3|wav)", name):
        return None
    return os.path.join(settings.UPLOAD_DIR, "audio", video_id, name)


def remove_clip_file(video_id: str, audio_url: Optional[str]) -> None:
    """Delete the mp3 behind an audio_url (used for quips the scheduler dropped). Never raises."""
    path = clip_path(video_id, audio_url)
    if path:
        _remove_quietly(path)


_piper_voice = None
_piper_voice_path = None
_piper_lock = threading.Lock()


def piper_model_path() -> str:
    """PIPER_MODEL_PATH as an absolute path; a relative one is taken from the repo root."""
    path = settings.PIPER_MODEL_PATH
    return path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)


def piper_available() -> bool:
    return PiperVoice is not None and os.path.isfile(piper_model_path())


def _synthesize_piper_sync(text: str, path: str) -> None:
    global _piper_voice, _piper_voice_path
    model = piper_model_path()
    with _piper_lock:  # one onnx session, one synthesis at a time
        if _piper_voice is None or _piper_voice_path != model:
            _piper_voice = PiperVoice.load(model)
            _piper_voice_path = model
        config = SynthesisConfig(length_scale=settings.PIPER_LENGTH_SCALE)
        with wave.open(path, "wb") as wav_file:
            _piper_voice.synthesize_wav(text, wav_file, config)


async def _synthesize(text: str, path: str) -> None:
    if settings.TTS_ENGINE == "piper":
        await asyncio.wait_for(asyncio.to_thread(_synthesize_piper_sync, text, path), timeout=settings.TTS_TIMEOUT_SECONDS)
    else:
        communicate = Communicate(text, settings.TTS_VOICE, rate=settings.TTS_RATE)
        await asyncio.wait_for(communicate.save(path), timeout=settings.TTS_TIMEOUT_SECONDS)


async def generate_audio_clip(quip: Dict[str, Any], video_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Synthesize one quip. Returns the quip with audio_url and duration_sec, or None if no usable audio was produced."""
    timestamp = float(quip.get("timestamp", 0.0))
    text = str(quip.get("text", ""))
    video_id = video_id or "default"

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video_id):
        raise ValueError(f"Invalid video_id: {video_id}")

    if not text.strip():
        logger.warning("Skipping TTS for empty text at timestamp %s", timestamp)
        return None
    piper = settings.TTS_ENGINE == "piper"
    if not settings.TTS_ENABLED:
        logger.warning("TTS is disabled; no audio for t=%.2f", timestamp)
        return None
    if piper and not piper_available():
        logger.warning("TTS_ENGINE=piper but piper-tts or the voice model (%s) is missing; no audio for t=%.2f", piper_model_path(), timestamp)
        return None
    if not piper and Communicate is None:
        logger.warning("edge-tts is not installed; no audio for t=%.2f", timestamp)
        return None

    audio_dir = os.path.join(settings.UPLOAD_DIR, "audio", video_id)
    os.makedirs(audio_dir, exist_ok=True)
    text_hash = hashlib.sha1(text.encode()).hexdigest()[:8]
    audio_filename = f"audio_{timestamp:.2f}_{text_hash}.{'wav' if piper else 'mp3'}"
    audio_path = os.path.join(audio_dir, audio_filename)

    attempts = 1 + max(0, settings.TTS_RETRIES)
    for attempt in range(1, attempts + 1):
        try:
            await _synthesize(text, audio_path)
            if not os.path.exists(audio_path) or os.path.getsize(audio_path) < MIN_AUDIO_BYTES:
                raise RuntimeError("TTS produced an empty or tiny file")
            break
        except Exception as e:
            if attempt < attempts and _is_transient(e):
                logger.warning("TTS attempt %d failed for t=%.2f (%s: %s); retrying", attempt, timestamp, type(e).__name__, e)
                _remove_quietly(audio_path)
                await asyncio.sleep(settings.TTS_RETRY_DELAY_SECONDS)
                continue
            _log_failure(e, timestamp, text, audio_path)
            _remove_quietly(audio_path)
            return None

    duration = measure_duration(audio_path)
    if duration is None:  # the file may not be fully flushed yet; look once more
        await asyncio.sleep(DURATION_RECHECK_DELAY_SECONDS)
        duration = measure_duration(audio_path)
    if duration is None:
        try:
            with open(audio_path, "rb") as f:
                head = f.read(4).hex()
        except OSError:
            head = "unreadable"
        logger.warning(
            "Discarding audio for t=%.2f: duration unreadable (text_len=%d voice=%s rate=%s file_bytes=%s first4=%s)",
            timestamp, len(text), settings.TTS_VOICE, settings.TTS_RATE, _file_size(audio_path), head,
        )
        _remove_quietly(audio_path)
        return None

    audio_url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/uploads/audio/{video_id}/{audio_filename}"
    return {"timestamp": timestamp, "text": text, "audio_url": audio_url, "duration_sec": duration}

