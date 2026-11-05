"""Synthesize a few test lines through the app's own TTS code path and print OK/fail with byte sizes.

Usage (from the repo root, venv active):
    python backend/scripts/tts_check.py
Uses the current settings (backend/.env): TTS_ENGINE (edge needs internet; piper and kokoro run offline), voice, rate, retries, timeout.
"""
import asyncio
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.config import settings  # noqa: E402
from app.services import tts  # noqa: E402

LINES = [
    "And the lion makes a grand entrance!",
    "That drum is working overtime.",
    "He just bowed to the crowd, so polite.",
    "Watch those feet, absolutely fearless.",
    "The lion blinks. The crowd gasps.",
    "Somebody check on the drummer's arms.",
]
VIDEO_ID = "ttscheck"


async def main() -> int:
    voice = {"kokoro": settings.KOKORO_VOICE, "piper": settings.PIPER_MODEL_PATH}.get(settings.TTS_ENGINE, settings.TTS_VOICE)
    print(f"engine={settings.TTS_ENGINE} voice={voice} rate={settings.TTS_RATE} retries={settings.TTS_RETRIES} timeout={settings.TTS_TIMEOUT_SECONDS}s")
    audio_dir = os.path.join(settings.UPLOAD_DIR, "audio", VIDEO_ID)
    failures = 0
    for i, text in enumerate(LINES, 1):
        out = await tts.generate_audio_clip({"timestamp": float(i), "text": text}, video_id=VIDEO_ID)
        if out is None:
            failures += 1
            print(f"{i}: FAIL  {text!r} (see log lines above)")
            continue
        path = os.path.join(audio_dir, os.path.basename(out["audio_url"]))
        print(f"{i}: OK    {os.path.getsize(path)} bytes, {out['duration_sec']:.2f}s")
    shutil.rmtree(audio_dir, ignore_errors=True)
    print(f"{len(LINES) - failures}/{len(LINES)} ok")
    return 1 if failures else 0


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main()))
