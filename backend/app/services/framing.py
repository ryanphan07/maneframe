"""Automatic framing for the 9:16 export. Pure functions: per-segment motion boxes (video_processor.motion_track) in,
a smoothed pan of a fixed-size crop window, the crop-or-fit choice and the caption zones out."""

import math
from typing import Callable, List, Optional, Sequence, Tuple

Box = Tuple[float, float, float, float]  # (x0, y0, x1, y1) as fractions of the frame

SEG_SEC = 1.5  # one motion box per segment
MAX_PAN_SPEED = 0.7  # crop windows per second the window may move
DEADBAND = 0.12  # a target closer than this many windows to the current position is ignored, so the pan does not jitter
# The action counts as wider than the window when its box is this many windows wide; if that holds for at least
# WIDE_SHARE of the segments that show motion, the whole frame is fitted instead of cropped (set from hoco_40s and
# the 5 eval clips, see the s51 handoff)
WIDE_RATIO = 1.0
WIDE_SHARE = 0.5
BOTTOM_BAND = (0.66, 0.82)  # caption bands as fractions of the output height: the lower third, above the app UI,
TOP_BAND = (0.10, 0.26)  # and the same distance below the top
CAPTION_BOTTOM_MARGIN = 0.2
CAPTION_TOP_MARGIN = 0.1


def segment_centers(boxes: Sequence[Optional[Box]]) -> List[float]:
    """Horizontal center of each segment's box; a segment without one holds the nearest earlier center (the first known
    one for a leading gap, the frame center when there is none at all)."""
    known = [(b[0] + b[2]) / 2 if b else None for b in boxes]
    first = next((c for c in known if c is not None), 0.5)
    result, last = [], first
    for c in known:
        last = last if c is None else c
        result.append(last)
    return result


def smooth_centers(centers: Sequence[float], seg_sec: float, window_frac: float) -> List[float]:
    """Follow the targets with a dead band and a speed limit: the window stays put for small changes and never moves
    faster than MAX_PAN_SPEED windows per second."""
    if not centers:
        return []
    max_step = MAX_PAN_SPEED * window_frac * seg_sec
    pos = centers[0]
    result = [pos]
    for target in centers[1:]:
        if abs(target - pos) > DEADBAND * window_frac:
            pos += max(-max_step, min(max_step, target - pos))
        result.append(pos)
    return result


def choose_mode(boxes: Sequence[Optional[Box]], window_frac: float) -> str:
    """"fit" when the action is wider than the crop window for at least WIDE_SHARE of the segments with motion,
    otherwise "crop". No motion at all is "crop" (a still window)."""
    widths = [b[2] - b[0] for b in boxes if b]
    if not widths:
        return "crop"
    wide = sum(1 for w in widths if w > WIDE_RATIO * window_frac)
    return "fit" if wide / len(widths) >= WIDE_SHARE else "crop"


def pan_keyframes(centers: Sequence[float], seg_sec: float, length: float, width: int, crop_w: int) -> List[Tuple[float, float]]:
    """(time, x in pixels) for the crop window: one per segment at its middle, clamped to the frame, plus the two ends
    of the clip held flat. Segment i covers [i * seg_sec, (i + 1) * seg_sec) of the output."""
    def x_of(c):
        return min(max(c * width - crop_w / 2, 0.0), float(width - crop_w))

    points = [(min((i + 0.5) * seg_sec, length), x_of(c)) for i, c in enumerate(centers)]
    if not points:
        return [(0.0, x_of(0.5)), (length, x_of(0.5))]
    inner = [p for p in points if 0 < p[0] < length]
    first = points[0][1] if points[0][0] >= 0 else x_of(0.5)
    last = points[-1][1]
    return [(0.0, first)] + inner + [(length, last)]


def _num(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") if abs(value - round(value)) > 1e-9 else str(int(round(value)))


def _seg_expr(a: Tuple[float, float], b: Tuple[float, float]) -> str:
    (t0, x0), (t1, x1) = a, b
    if t1 - t0 < 1e-6 or abs(x1 - x0) < 0.05:
        return _num(x0)
    slope = (x1 - x0) / (t1 - t0)
    return f"{_num(x0)}+{slope:.3f}*(t-{t0:.3f})"


def pan_expression(keyframes: Sequence[Tuple[float, float]]) -> str:
    """An ffmpeg expression in t that interpolates the keyframes linearly. A plain number when the window never moves.
    The segments are nested as a balanced if() tree, so a 30-minute clip stays about 11 levels deep."""
    if all(abs(x - keyframes[0][1]) < 0.05 for _, x in keyframes):
        return _num(keyframes[0][1])
    segs = list(zip(keyframes[:-1], keyframes[1:]))

    def build(lo: int, hi: int) -> str:
        if lo == hi:
            return _seg_expr(*segs[lo])
        mid = (lo + hi) // 2
        return f"if(lt(t,{segs[mid][1][0]:.3f}),{build(lo, mid)},{build(mid + 1, hi)})"

    return build(0, len(segs) - 1)


def window_frac_of(width: int, crop_w: int) -> float:
    return crop_w / width


def performer_y_ranges(
    boxes: Sequence[Optional[Box]], seg_sec: float, start: float, end: float, to_output: Callable[[float], float],
) -> List[Tuple[float, float]]:
    """Vertical extent of the performers (output-height fractions via `to_output`) in each segment overlapping [start, end)."""
    ranges = []
    for i, b in enumerate(boxes):
        if b and i * seg_sec < end and (i + 1) * seg_sec > start:
            ranges.append((to_output(b[1]), to_output(b[3])))
    return ranges


def caption_zone(ranges: Sequence[Tuple[float, float]]) -> str:
    """"bottom" (the default lower third) unless the performers cover it more than the top band; ties keep the bottom."""
    def overlap(band):
        return sum(max(0.0, min(hi, band[1]) - max(lo, band[0])) for lo, hi in ranges)

    return "top" if overlap(TOP_BAND) < overlap(BOTTOM_BAND) else "bottom"


def zone_chooser(
    boxes: Sequence[Optional[Box]], seg_sec: float, to_output: Callable[[float], float],
) -> Callable[[float, float], str]:
    """A function (caption start, end) -> "bottom" | "top" for build_ass."""
    return lambda start, end: caption_zone(performer_y_ranges(boxes, seg_sec, start, end, to_output))
