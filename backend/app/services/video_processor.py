import base64
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from app.config import settings

logger = logging.getLogger("maneframe.video")


class UnusableVideo(ValueError):
    """The video cannot be sampled; the message is safe to show to the user."""

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi"}

Box = Tuple[float, float, float, float]  # (x0, y0, x1, y1) as fractions of the frame


def sanitize_filename(filename: str) -> str:
    """
    Sanitize incoming filenames to prevent path traversal attacks.
    Strips directory paths and restricts characters to alphanumeric, underscores, hyphens, and periods.
    """
    basename = os.path.basename(filename)
    sanitized = re.sub(r"[^a-zA-Z0-9_\.-]", "_", basename)
    return sanitized.lstrip(".")


def validate_upload_metadata(filename: str, file_size: int) -> None:
    """
    Validates file extension and file size.
    Raises ValueError on failure to be caught by route handlers.
    """
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Invalid file extension '{ext}'. Allowed extensions are: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    if file_size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"File size exceeds the maximum limit of {settings.MAX_UPLOAD_MB}MB.")


def get_video_duration(file_path: str) -> float:
    """Return the duration in seconds. Raises ValueError if unreadable or fps/frame count are invalid."""
    cap = cv2.VideoCapture(file_path)
    try:
        if not cap.isOpened():
            raise ValueError("The uploaded video could not be read.")
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if not fps or fps <= 0 or not frame_count or frame_count <= 0:
            raise ValueError("The uploaded video could not be read.")
        return frame_count / fps
    finally:
        cap.release()


def is_video_corrupt(file_path: str) -> bool:
    """
    Verifies video file integrity using OpenCV by ensuring the container opens
    and at least the initial frame can be decoded successfully.
    """
    cap = cv2.VideoCapture(file_path)
    if not cap.isOpened():
        return True

    ret, frame = cap.read()
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    if not ret or frame is None or frame.size == 0 or frame_count <= 0:
        return True

    return False


def _probe_frames(cap, duration: float, probe: int):
    """Yield `probe` frames at evenly spaced times in one forward pass: grab() skips the frames in between
    without converting them, which is far cheaper than a keyframe seek and decode per sample."""
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if not fps or fps <= 0 or total <= 0:
        return
    targets = sorted({min(total - 1, int(duration * (i + 0.5) / probe * fps)) for i in range(probe)})
    index = 0
    for target in targets:
        while index < target:
            if not cap.grab():
                return
            index += 1
        ok, frame = cap.read()
        index += 1
        if not ok or frame is None:
            return
        yield frame


def motion_box(cap, duration, probe=30, probe_w=320, thresh=30, min_hits=3, min_brightness=50) -> Optional[Box]:
    """Return (x0, y0, x1, y1) as fractions of the frame covering where the bright, moving subjects are.

    Motion is counted only where the scene is lit (dark audience areas full of tiny phone lights and
    sensor noise are ignored), and speckle is removed with a morphological opening. Returns None when
    there is too little motion or the box would cover more than 80% of the frame.
    """
    grays = []
    for frame in _probe_frames(cap, duration, probe):
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (probe_w, max(1, int(h * probe_w / w))), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        grays.append(cv2.GaussianBlur(gray, (5, 5), 0).astype(np.float32))
    if len(grays) < 4:
        return None
    stack = np.stack(grays)
    background = np.median(stack, axis=0)
    lit = stack.max(axis=0) > min_brightness
    hits = ((np.abs(stack - background) > thresh) & lit).sum(axis=0)
    mask = (hits >= min_hits).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if mask.sum() < 20:
        return None
    gh, gw = mask.shape

    def span(profile, keep=0.2, smooth=7, gap=10):
        """Largest high-energy run in a 1-D motion profile (ignores isolated flicker far from the action)."""
        profile = np.convolve(profile.astype(np.float32), np.ones(smooth) / smooth, mode="same")
        idx = np.nonzero(profile >= keep * profile.max())[0]
        runs, start, prev = [], idx[0], idx[0]
        for i in idx[1:]:
            if i - prev > gap:
                runs.append((start, prev))
                start = i
            prev = i
        runs.append((start, prev))
        best = max(runs, key=lambda r: profile[r[0]:r[1] + 1].sum())
        return best[0], best[1] + 1

    cx0, cx1 = span(mask.sum(axis=0))
    cy0, cy1 = span(mask.sum(axis=1))
    x0, x1, y0, y1 = cx0 / gw, cx1 / gw, cy0 / gh, cy1 / gh
    # pad around the box, enforce a minimum size, clamp to the frame
    bw, bh = x1 - x0, y1 - y0
    x0, x1 = x0 - 0.12 * bw - 0.02, x1 + 0.12 * bw + 0.02
    y0, y1 = y0 - 0.12 * bh - 0.02, y1 + 0.12 * bh + 0.02
    if x1 - x0 < 0.35:
        mid = (x0 + x1) / 2
        x0, x1 = mid - 0.175, mid + 0.175
    if y1 - y0 < 0.25:
        mid = (y0 + y1) / 2
        y0, y1 = mid - 0.125, mid + 0.125
    x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1)
    if (x1 - x0) * (y1 - y0) > 0.8:  # motion everywhere: cropping would not help
        return None
    return float(x0), float(y0), float(x1), float(y1)


def compute_crop_box(video_path: str, duration: float) -> Optional[Box]:
    """Compute the crop box once per video. None means use the full frame."""
    cap = cv2.VideoCapture(video_path)
    try:
        if not cap.isOpened():
            raise ValueError("Error opening video file")
        box = motion_box(cap, duration)
    finally:
        cap.release()
    if box:
        logger.info("Crop box (x0,y0,x1,y1 fractions): %s", tuple(round(v, 3) for v in box))
    else:
        logger.info("Crop box: none, using the full frame")
    return box


def plan_windows(duration: float, window_sec: float, min_tail_sec: float = 2.0) -> List[Tuple[float, float]]:
    """Split [0, duration] into windows of window_sec; a tail shorter than min_tail_sec joins the previous window."""
    if duration <= 0 or window_sec <= 0:
        return []
    windows = []
    start = 0.0
    while start < duration:
        windows.append((start, min(start + window_sec, duration)))
        start += window_sec
    if len(windows) > 1 and windows[-1][1] - windows[-1][0] < min_tail_sec:
        tail = windows.pop()
        windows[-1] = (windows[-1][0], tail[1])
    return windows


def frame_count_for_window(start: float, end: float, max_frames: int) -> int:
    """Up to max_frames, but never more than about one per second of window."""
    return max(1, min(max_frames, math.ceil(end - start)))


def _frame_size(fw: int, fh: int, width: int) -> Tuple[int, int]:
    """Output size for a frame: `width` wide, but never more pixels than a 16:9 frame of that width. Image tokens grow
    with area, so a portrait phone clip at full width would overflow the model's context."""
    out_w, out_h = width, max(1, int(fh * width / fw))
    budget = width * width * 9 / 16
    if out_w * out_h > budget:
        scale = math.sqrt(budget / (out_w * out_h))
        out_w, out_h = max(1, int(out_w * scale)), max(1, int(out_h * scale))
    return out_w, out_h


def sample_window_frames(
    video_path: str,
    start: float,
    end: float,
    max_frames: int,
    width: int,
    box: Optional[Box] = None,
) -> List[Dict[str, Any]]:
    """Return evenly spaced frames from [start, end) as [{"timestamp", "image_data"}] (base64 JPEG).

    The same crop box is applied to every frame, then each frame is resized to `width` keeping its aspect ratio.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError("Error opening video file")
    n = frame_count_for_window(start, end, max_frames)
    frames: List[Dict[str, Any]] = []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0:
            raise UnusableVideo("The video has no usable frame rate, so it cannot be analyzed.")
        times = [start + (end - start) * (i + 0.5) / n for i in range(n)]
        # One seek to the first sample, then a forward pass: grab() skips the frames in between without converting them,
        # which is much cheaper than a keyframe seek and decode per sample.
        cap.set(cv2.CAP_PROP_POS_MSEC, times[0] * 1000)
        index = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
        for t in times:
            target = int(t * fps + 0.5)  # the frame a seek to t lands on (nearest, halves up)
            while index < target and cap.grab():
                index += 1
            ok, frame = cap.read()
            index += 1
            if not ok or frame is None:
                logger.warning("Frame read failed at %.2fs in window %.1f-%.1fs; keeping the %d frames read so far", t, start, end, len(frames))
                break
            if box:
                h, w = frame.shape[:2]
                x0, y0, x1, y1 = box
                frame = frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
            fh, fw = frame.shape[:2]
            out_w, out_h = _frame_size(fw, fh, width)
            frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                frames.append({"timestamp": round(t, 2), "image_data": base64.b64encode(buf).decode()})
    finally:
        cap.release()
    return frames
