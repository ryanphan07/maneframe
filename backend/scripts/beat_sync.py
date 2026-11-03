"""Research prototype: how well do the performers' motion peaks line up with the drum beats?

Run from backend/ with the venv active (needs requirements-dev.txt):
    python scripts/beat_sync.py <video> [--out-dir DIR]

Extracts the audio with the export ffmpeg binary, detects beats and drum onsets with librosa, measures motion
energy inside the motion crop box per frame (frame differencing), finds motion peaks and reports each peak's signed
offset to the nearest beat (positive = the motion comes after the beat). Beat statistics cover only peaks inside the span the beat tracker covers. Because peaks and beats are both
roughly periodic, percent-within is also reported against a chance baseline (the peak train randomly circular-shifted).
Writes <name>.beat_sync.json and
<name>.beat_sync.png. Not wired into the app.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services import video_processor  # noqa: E402
from app.services.export import resolve_ffmpeg  # noqa: E402

SAMPLE_RATE = 22050
ANALYSIS_WIDTH = 320
DIFF_THRESHOLD = 20  # gray levels; below this a pixel change counts as sensor noise
MIN_BRIGHTNESS = 50  # same lit-scene rule as video_processor.motion_box
SMOOTH_SEC = 0.1
MIN_PEAK_GAP_SEC = 0.2
PEAK_PROMINENCE = 0.5  # in standard deviations of the smoothed motion energy
WITHIN_SEC = 0.1


def extract_audio(video_path: str, wav_path: str) -> None:
    cmd = [resolve_ffmpeg(), "-y", "-v", "error", "-i", video_path, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), wav_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not Path(wav_path).is_file():
        raise RuntimeError(f"ffmpeg could not extract audio: {result.stderr.strip()[-300:]}")


def motion_energy(video_path: str, box: Optional[tuple]) -> tuple:
    """Return (fps, energy) with one value per frame: the fraction of lit pixels in the crop box that changed
    since the previous frame. One sequential pass, frames downscaled before differencing."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("OpenCV could not open the video.")
    fps = cap.get(cv2.CAP_PROP_FPS)
    prev, energy = None, []
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            h, w = frame.shape[:2]
            if box:
                x0, y0, x1, y1 = box
                frame = frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
                h, w = frame.shape[:2]
            small = cv2.resize(frame, (ANALYSIS_WIDTH, max(1, int(h * ANALYSIS_WIDTH / w))), interpolation=cv2.INTER_AREA)
            gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
            if prev is None:
                energy.append(0.0)
            else:
                lit = np.maximum(gray, prev) > MIN_BRIGHTNESS
                changed = (cv2.absdiff(gray, prev) > DIFF_THRESHOLD) & lit
                energy.append(float(changed.mean()))
            prev = gray
    finally:
        cap.release()
    return float(fps), np.asarray(energy, dtype=np.float64)


def find_motion_peaks(energy: np.ndarray, fps: float) -> tuple:
    """Return (peak_times_sec, smoothed_energy). Peaks are local maxima at least MIN_PEAK_GAP_SEC apart that
    stand PEAK_PROMINENCE standard deviations above their surroundings."""
    from scipy.signal import find_peaks

    width = max(1, int(round(SMOOTH_SEC * fps)))
    smooth = np.convolve(energy, np.ones(width) / width, mode="same")
    if len(smooth) < 3 or smooth.std() == 0:
        return np.array([]), smooth
    idx, _ = find_peaks(smooth, distance=max(1, int(MIN_PEAK_GAP_SEC * fps)), prominence=PEAK_PROMINENCE * smooth.std())
    return idx / fps, smooth


def nearest_offsets(peaks: np.ndarray, refs: np.ndarray) -> np.ndarray:
    """Signed offset (peak - nearest reference) for each peak, in seconds."""
    peaks, refs = np.asarray(peaks, dtype=float), np.asarray(refs, dtype=float)
    if len(peaks) == 0 or len(refs) == 0:
        return np.array([])
    return np.array([p - refs[np.argmin(np.abs(refs - p))] for p in peaks])


def summarize(offsets: np.ndarray, within_sec: float = WITHIN_SEC) -> Dict[str, Any]:
    if len(offsets) == 0:
        return {"count": 0, "mean": None, "median": None, "mean_abs": None, "percent_within": None}
    return {
        "count": int(len(offsets)),
        "mean": round(float(offsets.mean()), 4),
        "median": round(float(np.median(offsets)), 4),
        "mean_abs": round(float(np.abs(offsets).mean()), 4),
        "percent_within": round(float((np.abs(offsets) <= within_sec).mean() * 100), 1),
    }


def covered_by(peaks: np.ndarray, refs: np.ndarray) -> np.ndarray:
    """Peaks inside the span the references cover (first to last, plus half the median gap). The beat tracker can
    stop early or start late; a peak far outside its span has no meaningful nearest beat."""
    if len(refs) < 2 or len(peaks) == 0:
        return np.asarray(peaks, dtype=float)
    pad = float(np.median(np.diff(refs))) / 2
    return peaks[(peaks >= refs[0] - pad) & (peaks <= refs[-1] + pad)]


def chance_percent_within(peaks: np.ndarray, refs: np.ndarray, duration: float, within_sec: float = WITHIN_SEC,
                          trials: int = 1000, seed: int = 0) -> Dict[str, float]:
    """Null model: slide the whole peak train by a random circular shift (keeps its spacing, destroys its alignment
    with the audio) and report how often a peak lands within within_sec of a reference anyway."""
    if len(peaks) == 0 or len(refs) == 0 or duration <= 0:
        return {"mean": None, "p95": None}
    rng = np.random.default_rng(seed)
    scores = []
    for shift in rng.uniform(0, duration, trials):
        shifted = (peaks + shift) % duration
        scores.append((np.abs(nearest_offsets(shifted, refs)) <= within_sec).mean() * 100)
    return {"mean": round(float(np.mean(scores)), 1), "p95": round(float(np.percentile(scores, 95)), 1)}


def detect_audio(y: np.ndarray, sr: int) -> Dict[str, Any]:
    import librosa

    hop = 512
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    env_times = librosa.times_like(env, sr=sr, hop_length=hop)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=env, sr=sr, hop_length=hop)
    onset_frames = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=hop)
    return {
        "tempo_bpm": float(np.atleast_1d(tempo)[0]),
        "beats": librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop),
        "onsets": librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop),
        "env": env,
        "env_times": env_times,
    }


def analyze_signals(y: np.ndarray, sr: int, energy: np.ndarray, fps: float) -> Dict[str, Any]:
    """Core of the prototype, separate from file I/O so it can be tested on synthetic signals."""
    audio = detect_audio(y, sr)
    peaks, smooth = find_motion_peaks(energy, fps)
    beat_peaks = covered_by(peaks, audio["beats"])
    beat_offsets = nearest_offsets(beat_peaks, audio["beats"])
    duration = len(energy) / fps
    onset_offsets = nearest_offsets(peaks, audio["onsets"])
    return {
        "tempo_bpm": round(audio["tempo_bpm"], 1),
        "beats": audio["beats"],
        "onsets": audio["onsets"],
        "peaks": peaks,
        "beat_offsets": beat_offsets,
        "onset_offsets": onset_offsets,
        "beat_summary": summarize(beat_offsets),
        "onset_summary": summarize(onset_offsets),
        "beat_chance": chance_percent_within(beat_peaks, audio["beats"], duration),
        "onset_chance": chance_percent_within(peaks, audio["onsets"], duration),
        "env": audio["env"],
        "env_times": audio["env_times"],
        "motion_smooth": smooth,
    }


def plot(result: Dict[str, Any], fps: float, title: str, png_path: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(14, 6), sharex=True)
    top.plot(result["env_times"], result["env"], color="#1f77b4", lw=0.8)
    for b in result["beats"]:
        top.axvline(b, color="#d62728", alpha=0.5, lw=0.8)
    top.set_ylabel("audio onset strength")
    top.set_title(f"{title}: tempo {result['tempo_bpm']} BPM, red lines = beats")
    t = np.arange(len(result["motion_smooth"])) / fps
    bottom.plot(t, result["motion_smooth"], color="#2ca02c", lw=0.8)
    bottom.plot(result["peaks"], np.interp(result["peaks"], t, result["motion_smooth"]), "kv", ms=5, label="motion peaks")
    for b in result["beats"]:
        bottom.axvline(b, color="#d62728", alpha=0.35, lw=0.8)
    bottom.set_ylabel("motion energy")
    bottom.set_xlabel("seconds")
    bottom.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(png_path, dpi=110)
    plt.close(fig)


def run(video_path: str, out_dir: str) -> Dict[str, Any]:
    import librosa

    duration = video_processor.get_video_duration(video_path)
    box = video_processor.compute_crop_box(video_path, duration)
    with tempfile.TemporaryDirectory() as tmp:
        wav = str(Path(tmp) / "audio.wav")
        extract_audio(video_path, wav)
        y, sr = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
    fps, energy = motion_energy(video_path, box)
    result = analyze_signals(y, sr, energy, fps)

    stem = Path(video_path).stem
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    png_path = out / f"{stem}.beat_sync.png"
    plot(result, fps, stem, str(png_path))
    r3 = lambda a: [round(float(v), 3) for v in a]  # noqa: E731
    report = {
        "video": Path(video_path).name,
        "duration_sec": round(duration, 2),
        "fps": round(fps, 2),
        "crop_box": list(box) if box else None,
        "tempo_bpm": result["tempo_bpm"],
        "within_sec": WITHIN_SEC,
        "beats": r3(result["beats"]),
        "onsets": r3(result["onsets"]),
        "peaks": r3(result["peaks"]),
        "offsets": r3(result["beat_offsets"]),
        "onset_offsets": r3(result["onset_offsets"]),
        "beat_offsets_summary": result["beat_summary"],
        "onset_offsets_summary": result["onset_summary"],
        "beat_chance_percent_within": result["beat_chance"],
        "onset_chance_percent_within": result["onset_chance"],
    }
    (out / f"{stem}.beat_sync.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("video")
    ap.add_argument("--out-dir", default=str(BACKEND_DIR.parent / "docs" / "research"))
    args = ap.parse_args(argv)
    report = run(args.video, args.out_dir)
    print(json.dumps({k: v for k, v in report.items() if k not in ("beats", "onsets", "peaks", "offsets", "onset_offsets")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
