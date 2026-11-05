"""Render the same six lines in several Kokoro voices for auditioning.

Usage (from the repo root, venv active):
    python backend/scripts/voice_samples.py [--voices af_nicole,af_bella,"af_nicole:0.7,af_bella:0.3" ...] [--out logs/voices]
Writes <out>/<voice>/NN.wav and <out>/index.html with a player per line and voice. --voices takes names
separated by spaces; a blend contains a comma, so quote it (or pass it as its own argument).
Uses the app's own Kokoro path (KOKORO_* settings from backend/.env, gain included); needs kokoro-onnx and the model files.
"""
import argparse
import html
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.config import settings  # noqa: E402
from app.services import tts  # noqa: E402

LINES = [
    "Three lions crouched low. Chat, are they buffering?",
    "Blue lion reared up. Pure aura farming.",
    "All three lions down low. Leg day, no skips.",
    "Pink lion alone at center. Main character energy.",
    "Yellow lion on the red envelope. Fanum tax collected.",
    "Empty stage. The lions logged off.",
]
DEFAULT_VOICES = ["af_nicole", "af_bella", "af_heart", "bm_george", "bm_lewis", "am_santa", "af_nicole:0.7,af_bella:0.3"]


def folder_name(voice: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", voice.replace(":", "-")).strip("_")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--voices", nargs="+", default=DEFAULT_VOICES, help="voice names or blends like af_nicole:0.7,af_bella:0.3")
    ap.add_argument("--out", default=os.path.join(tts.REPO_ROOT, "logs", "voices"))
    args = ap.parse_args()
    if not tts.kokoro_available():
        print(f"kokoro-onnx or its files are missing ({tts.kokoro_model_path()}, {tts.kokoro_voices_path()}); see README", file=sys.stderr)
        return 1
    valid = tts.kokoro_voice_names()
    for voice in args.voices:
        try:
            tts.parse_kokoro_voice(voice, valid)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
    rows = []
    failures = 0
    for voice in args.voices:
        settings.KOKORO_VOICE = voice
        folder = folder_name(voice)
        os.makedirs(os.path.join(args.out, folder), exist_ok=True)
        cells = []
        for n, line in enumerate(LINES, 1):
            rel = f"{folder}/{n:02d}.wav"
            path = os.path.join(args.out, rel)
            try:
                tts._synthesize_kokoro_sync(line, path)
                cells.append(f'<td><audio controls preload="none" src="{html.escape(rel)}"></audio></td>')
            except Exception as e:
                failures += 1
                tts._remove_quietly(path)
                print(f"{voice} line {n}: FAIL {type(e).__name__}: {e}", file=sys.stderr)
                cells.append("<td>failed</td>")
        print(f"{voice}: done")
        rows.append(f"<tr><th>{html.escape(voice)}</th>{''.join(cells)}</tr>")
    head = "".join(f"<th>{n}. {html.escape(line)}</th>" for n, line in enumerate(LINES, 1))
    page = (
        '<!doctype html><meta charset="utf-8"><title>Kokoro voices</title>'
        "<style>body{font:14px sans-serif}td,th{padding:4px 8px;vertical-align:top;text-align:left}audio{width:220px}</style>"
        f"<table><tr><th>voice</th>{head}</tr>{''.join(rows)}</table>"
    )
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {os.path.join(args.out, 'index.html')}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
