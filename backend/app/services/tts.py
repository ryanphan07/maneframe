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

try:
    from kokoro_onnx import Kokoro
except ImportError:  # kokoro-onnx is optional (TTS_ENGINE=kokoro)
    Kokoro = None

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

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
        timestamp, type(exc).__name__, exc, len(text), _voice_label(), settings.TTS_RATE, _file_size(path),
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


KOKORO_PEAK_LIMIT_DBFS = -1.0
_kokoro_engine = None
_kokoro_key = None
_kokoro_voice = None  # a voice name, or the blended style array
_kokoro_lock = threading.Lock()


def _repo_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)


def kokoro_model_path() -> str:
    return _repo_path(settings.KOKORO_MODEL_PATH)


def kokoro_voices_path() -> str:
    return _repo_path(settings.KOKORO_VOICES_PATH)


def kokoro_available() -> bool:
    return Kokoro is not None and np is not None and os.path.isfile(kokoro_model_path()) and os.path.isfile(kokoro_voices_path())


def parse_kokoro_voice(spec: str, valid: Optional[List[str]] = None) -> List[tuple]:
    """'af_nicole' or 'af_nicole:0.7,af_bella:0.3' -> [(name, weight)] with weights normalized to sum 1.
    Raises ValueError on a malformed spec, and (when `valid` is given) on an unknown voice, listing the valid ones."""
    parts = [p.strip() for p in (spec or "").split(",") if p.strip()]
    if not parts:
        raise ValueError("KOKORO_VOICE is empty")
    voices = []
    for part in parts:
        name, sep, weight_text = part.partition(":")
        name = name.strip()
        try:
            weight = float(weight_text) if sep else 1.0
        except ValueError:
            raise ValueError(f"KOKORO_VOICE: bad weight {weight_text!r} for voice {name!r}") from None
        if not name or not (0 < weight < float("inf")):
            raise ValueError(f"KOKORO_VOICE: bad entry {part!r} (want name or name:weight with weight > 0)")
        voices.append((name, weight))
    if valid is not None:
        unknown = [n for n, _ in voices if n not in valid]
        if unknown:
            raise ValueError(f"KOKORO_VOICE: unknown voice {', '.join(unknown)}; valid voices: {', '.join(sorted(valid))}")
    total = sum(w for _, w in voices)
    return [(n, w / total) for n, w in voices]


def kokoro_voice_names() -> List[str]:
    """Voice names stored in the voices file (read without loading the model)."""
    with np.load(kokoro_voices_path()) as data:
        return list(data.files)


def validate_tts_config() -> None:
    """Startup check for TTS_ENGINE=kokoro: a malformed or unknown voice is an error (with the valid list).
    A missing package or model file is not: that gives 'no audio' plus a log line per clip, like the other engines."""
    if settings.TTS_ENGINE != "kokoro" or not settings.TTS_ENABLED:
        return
    if kokoro_available():
        parse_kokoro_voice(settings.KOKORO_VOICE, kokoro_voice_names())
    else:
        parse_kokoro_voice(settings.KOKORO_VOICE)
        logger.warning("TTS_ENGINE=kokoro but kokoro-onnx or its files (%s, %s) are missing; clips will have no audio", kokoro_model_path(), kokoro_voices_path())


def apply_gain(samples, gain_db: float, peak_limit_dbfs: float = KOKORO_PEAK_LIMIT_DBFS):
    """Scale float samples by gain_db, then scale down if the peak would pass peak_limit_dbfs. No other processing."""
    out = samples * (10.0 ** (gain_db / 20.0))
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    ceiling = 10.0 ** (peak_limit_dbfs / 20.0)
    if peak > ceiling:
        out = out * (ceiling / peak)
    return out


def _load_kokoro():
    """(engine, voice) for the current settings, loaded once. Caller holds _kokoro_lock."""
    global _kokoro_engine, _kokoro_key, _kokoro_voice
    key = (kokoro_model_path(), kokoro_voices_path(), settings.KOKORO_VOICE)
    if _kokoro_engine is None or _kokoro_key != key:
        engine = Kokoro(key[0], key[1])  # CPU onnxruntime only: the GPU belongs to llama-server
        voices = parse_kokoro_voice(settings.KOKORO_VOICE, list(engine.get_voices()))
        if len(voices) == 1:
            voice = voices[0][0]
        else:
            voice = sum(w * engine.get_voice_style(n) for n, w in voices)
        _kokoro_engine, _kokoro_key, _kokoro_voice = engine, key, voice
    return _kokoro_engine, _kokoro_voice


def _synthesize_kokoro_sync(text: str, path: str) -> None:
    with _kokoro_lock:  # one onnx session, one synthesis at a time
        engine, voice = _load_kokoro()
        samples, rate = engine.create(text, voice=voice, speed=settings.KOKORO_SPEED, lang=settings.KOKORO_LANG)
    samples = apply_gain(np.asarray(samples, dtype=np.float32), settings.KOKORO_GAIN_DB)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(int(rate))
        wav_file.writeframes(pcm.tobytes())


def _voice_label() -> str:
    return settings.KOKORO_VOICE if settings.TTS_ENGINE == "kokoro" else settings.TTS_VOICE


async def _synthesize(text: str, path: str) -> None:
    if settings.TTS_ENGINE == "piper":
        await asyncio.wait_for(asyncio.to_thread(_synthesize_piper_sync, text, path), timeout=settings.TTS_TIMEOUT_SECONDS)
    elif settings.TTS_ENGINE == "kokoro":
        await asyncio.wait_for(asyncio.to_thread(_synthesize_kokoro_sync, text, path), timeout=settings.TTS_TIMEOUT_SECONDS)
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
    kokoro = settings.TTS_ENGINE == "kokoro"
    if not settings.TTS_ENABLED:
        logger.warning("TTS is disabled; no audio for t=%.2f", timestamp)
        return None
    if piper and not piper_available():
        logger.warning("TTS_ENGINE=piper but piper-tts or the voice model (%s) is missing; no audio for t=%.2f", piper_model_path(), timestamp)
        return None
    if kokoro and not kokoro_available():
        logger.warning("TTS_ENGINE=kokoro but kokoro-onnx or its files (%s, %s) are missing; no audio for t=%.2f", kokoro_model_path(), kokoro_voices_path(), timestamp)
        return None
    if not piper and not kokoro and Communicate is None:
        logger.warning("edge-tts is not installed; no audio for t=%.2f", timestamp)
        return None

    audio_dir = os.path.join(settings.UPLOAD_DIR, "audio", video_id)
    os.makedirs(audio_dir, exist_ok=True)
    text_hash = hashlib.sha1(text.encode()).hexdigest()[:8]
    audio_filename = f"audio_{timestamp:.2f}_{text_hash}.{'wav' if piper or kokoro else 'mp3'}"
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
            timestamp, len(text), _voice_label(), settings.TTS_RATE, _file_size(audio_path), head,
        )
        _remove_quietly(audio_path)
        return None

    audio_url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/uploads/audio/{video_id}/{audio_filename}"
    return {"timestamp": timestamp, "text": text, "audio_url": audio_url, "duration_sec": duration}

