"""Render a finished job as a shareable mp4: original video, original audio ducked under the quips, quip audio at its
scheduled timestamps, and optionally the quip text burned in as captions."""

import asyncio
import logging
import math
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2

from app.config import settings
from app.services import framing as framing_service
from app.services import tts as tts_service
from app.services import video_processor as video_processor_service

logger = logging.getLogger("maneframe.export")

# TTS clips are peaky (piper hits 0 dBFS at -20 LUFS), and loudnorm cannot raise them without clipping, so a gentle
# compressor evens them out first; measured spread of quip loudness 7 dB without it, 2.5 dB with it
QUIP_COMPRESSOR = "acompressor=threshold=-24dB:ratio=3:attack=5:release=80"
DUCK_MERGE_GAP_SEC = 0.5  # quips closer than this share one ducked stretch instead of pumping the volume
NO_AUDIO_CAPTION_SEC = 3.0
CRF = 23
CAPTIONS_FILE = "captions.ass"
GRAPH_FILE = "filter_graph.txt"
STDERR_TAIL = 2000
ASPECTS = ("original", "9:16")
VERTICAL_RATIO = 9 / 16
VERTICAL_MAX_HEIGHT = 1920
END_TOLERANCE_SEC = 0.5  # an end this far past the measured duration is player rounding, not a range outside the clip


class ExportError(Exception):
    """A render failed. The message is safe to show to the user; details are in the server log."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ExportQuip:
    timestamp: float
    text: str
    audio_path: Optional[str] = None
    duration_sec: Optional[float] = None


def resolve_ffmpeg() -> str:
    if settings.FFMPEG_PATH:
        if not os.path.isfile(settings.FFMPEG_PATH):
            raise ExportError("FFMPEG_PATH does not point to a file.")
        return settings.FFMPEG_PATH
    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        logger.exception("Could not locate a bundled ffmpeg")
        raise ExportError("No ffmpeg is available. Install requirements.txt or set FFMPEG_PATH.")
    return path


def ffmpeg_available() -> bool:
    try:
        resolve_ffmpeg()
    except ExportError:
        return False
    return True


# ---- pure builders ---------------------------------------------------------------------------


def _ass_time(seconds: float) -> str:
    centis = int(round(max(seconds, 0.0) * 100))
    return f"{centis // 360000}:{centis // 6000 % 60:02d}:{centis // 100 % 60:02d}.{centis % 100:02d}"


def _ass_text(text: str) -> str:
    """Drop what ASS would treat as markup (override braces, escapes); whitespace is collapsed."""
    return " ".join(re.sub(r"[{}\\]", "", text).split())


def caption_spans(quips: List[ExportQuip]) -> List[Tuple[float, float, str]]:
    """(start, end, text) per quip: its audio length, or 3 s without audio, never running into the next caption."""
    ordered = sorted(quips, key=lambda q: q.timestamp)
    spans = []
    for i, quip in enumerate(ordered):
        length = quip.duration_sec if quip.audio_path and quip.duration_sec else NO_AUDIO_CAPTION_SEC
        end = quip.timestamp + length
        if i + 1 < len(ordered):
            end = min(end, ordered[i + 1].timestamp)
        if end > quip.timestamp and _ass_text(quip.text):
            spans.append((quip.timestamp, end, _ass_text(quip.text)))
    return spans


def build_ass(
    quips: List[ExportQuip], width: int, height: int, vertical: bool = False,
    zone_for: Optional[Callable[[float, float], str]] = None,
) -> str:
    """An ASS subtitle file sized to the video: bottom center, white text on a translucent box. `vertical` (9:16) sizes
    the text from the width and lifts it into the lower third, above the area Instagram and TikTok cover with their UI.
    `zone_for(start, end)` may return "top" for a caption whose lower third would cover the performers (vertical only)."""
    if vertical:
        font_size = max(12, round(width * 0.062))
        margin_v = round(height * 0.2)
        margin_x = round(width * 0.08)
    else:
        font_size = max(12, round(min(width, height) * 0.055))
        margin_v = round(height * 0.06)
        margin_x = round(width * 0.06)
    pad = max(2, round(font_size * 0.25))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Default,Arial,{font_size},&H00FFFFFF,&H000000FF,&H99000000,&H99000000,-1,0,0,0,100,100,0,0,3,{pad},0,2,"
        f"{margin_x},{margin_x},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end, text in caption_spans(quips):
        margin, tag = 0, ""
        if vertical and zone_for and zone_for(start, end) == "top":
            margin, tag = round(height * framing_service.CAPTION_TOP_MARGIN), r"{\an8}"
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,{margin},,{tag}{text}")
    return "\n".join(lines) + "\n"


def duck_intervals(quips: List[ExportQuip]) -> List[Tuple[float, float]]:
    """Time ranges where the original audio is lowered: while a spoken quip plays. Near neighbours are merged."""
    spans = sorted((q.timestamp, q.timestamp + q.duration_sec) for q in quips if q.audio_path and q.duration_sec)
    merged: List[List[float]] = []
    for start, end in spans:
        if merged and start - merged[-1][1] <= DUCK_MERGE_GAP_SEC:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


VerticalCrop = Tuple[int, int, int, int, int, int]


def vertical_crop(width: int, height: int, box: Optional[Tuple[float, float, float, float]]) -> VerticalCrop:
    """(x, y, crop_w, crop_h, out_w, out_h) for a 9:16 window centered on the motion box (fractions x0, y0, x1, y1; None
    is the frame center), clamped to the frame. A landscape source keeps its full height; a source already narrower than
    9:16 keeps its full width. The output is 1080x1920, or the window's own size when that is smaller; all even."""
    if width / height > VERTICAL_RATIO:
        crop_h = height - height % 2
        crop_w = min(_even(crop_h * VERTICAL_RATIO), width - width % 2)
    else:
        crop_w = width - width % 2
        crop_h = min(_even(crop_w / VERTICAL_RATIO), height - height % 2)
    cx = width / 2 if box is None else (box[0] + box[2]) / 2 * width
    cy = height / 2 if box is None else (box[1] + box[3]) / 2 * height
    x = int(round(min(max(cx - crop_w / 2, 0), width - crop_w)))
    y = int(round(min(max(cy - crop_h / 2, 0), height - crop_h)))
    x, y = x - x % 2, y - y % 2
    out_h = min(VERTICAL_MAX_HEIGHT, crop_h)
    out_w = _even(out_h * VERTICAL_RATIO)
    return x, y, crop_w, crop_h, out_w, out_h


def vertical_fit(width: int, height: int) -> VerticalCrop:
    """The 9:16 canvas for the fit framing: the whole frame scaled to the canvas width, a blurred copy above and below.
    Same tuple shape as vertical_crop; the first four fields describe the full frame, out_w/out_h the canvas (at most
    1080 wide, never wider than the source, so nothing is upscaled)."""
    out_w = min(1080, width - width % 2)
    return 0, 0, width, height, out_w, _even(out_w / VERTICAL_RATIO)


def fit_video_height(width: int, height: int, out_w: int) -> int:
    return _even(height * out_w / width)


def vertical_y_mapper(mode: str, width: int, height: int, window: VerticalCrop) -> Callable[[float], float]:
    """Maps a y fraction of the source frame to a fraction of the 9:16 output height, for caption placement."""
    _, y, _, crop_h, out_w, out_h = window
    if mode == "fit":
        video_h = fit_video_height(width, height, out_w)
        return lambda v: ((out_h - video_h) / 2 + v * video_h) / out_h
    return lambda v: min(1.0, max(0.0, (v * height - y) / crop_h))


def plan_vertical(
    video_path: str, width: int, height: int, duration: float, start: float, length: float, framing: str = "auto",
) -> Tuple[VerticalCrop, Optional[str], bool, Optional[Callable[[float, float], str]]]:
    """Everything the 9:16 render needs: (window, pan expression for crop x or None, fit mode, caption zone function or
    None). The motion boxes of the rendered range (about 1.5 s segments) are tracked once. A source already as narrow as
    9:16 has nothing to pan or fit and keeps one window; "static" is the old single box for the whole video (dev only)."""
    base = vertical_crop(width, height, None)
    if framing == "static" or base[2] >= width - 1:
        box = video_processor_service.compute_crop_box(video_path, duration)
        return vertical_crop(width, height, box), None, False, None
    seg = framing_service.SEG_SEC
    boxes = video_processor_service.motion_track(video_path, duration, start, start + length, seg)
    window_frac = base[2] / width
    mode = framing_service.choose_mode(boxes, window_frac) if framing == "auto" else framing
    logger.info("9:16 framing: %s (%d segments, %d with motion)", mode, len(boxes), sum(1 for b in boxes if b))
    if mode == "fit":
        window = vertical_fit(width, height)
        pan = None
    else:
        window = base
        centers = framing_service.smooth_centers(framing_service.segment_centers(boxes), seg, window_frac)
        keys = framing_service.pan_keyframes(centers, seg, length, width, base[2])
        pan = framing_service.pan_expression(keys)
    zones = framing_service.zone_chooser(boxes, seg, vertical_y_mapper(mode, width, height, window))
    return window, pan, mode == "fit", zones


def normalize_range(start: Optional[float], end: Optional[float], duration: float) -> Tuple[Optional[float], Optional[float]]:
    """Validate a trim range against the clip. Returns (start, end) with None where the range reaches the clip's own
    start or end, so an untrimmed export keeps its cache entry and ffmpeg arguments. Raises ExportError (400)."""
    if start is not None and start < 0:
        raise ExportError("The start time cannot be negative.", 400)
    if start is not None and end is not None and start >= end:
        raise ExportError("The start time must be before the end time.", 400)
    if start is not None and start >= duration:
        raise ExportError(f"The start time is outside the video, which is {duration:.1f} s long.", 400)
    if end is not None and end > duration + END_TOLERANCE_SEC:
        raise ExportError(f"The end time is outside the video, which is {duration:.1f} s long.", 400)
    if end is not None and end >= duration - 0.001:
        end = None
    if start is not None and start <= 0.001:
        start = None
    return start, end


def select_trim_quips(quips: List[ExportQuip], start: Optional[float], end: Optional[float]) -> List[ExportQuip]:
    """The quips for a trimmed export, timestamps shifted by -start. Only a quip that starts inside [start, end) is kept,
    and one that would run past an explicit end (its audio length, or the 3 s caption without audio) is dropped, never
    cut. With no trim the list is returned untouched."""
    if start is None and end is None:
        return quips
    lo = start or 0.0
    kept = []
    for quip in quips:
        if quip.timestamp < lo or (end is not None and quip.timestamp >= end):
            continue
        length = quip.duration_sec if quip.audio_path and quip.duration_sec else NO_AUDIO_CAPTION_SEC
        if end is not None and quip.timestamp + length > end:
            continue
        kept.append(ExportQuip(quip.timestamp - lo, quip.text, quip.audio_path, quip.duration_sec))
    return kept


def build_filter_graph(
    duration: float,
    has_audio: bool,
    quips: List[ExportQuip],
    captions_file: Optional[str] = None,
    duck_db: Optional[float] = None,
    quip_lufs: Optional[float] = None,
    vertical: Optional[VerticalCrop] = None,
    pan_x: Optional[str] = None,
    fit: bool = False,
) -> str:
    """The -filter_complex graph. Each quip is loudness-normalized to `quip_lufs` (loudnorm) so both TTS engines land at
    the same level, and the original is lowered by `duck_db` while a quip plays; both default to the settings. `captions_file` is relative: ffmpeg runs with the job's export folder as its working
    directory, which keeps drive letters out of the subtitles filter's escaping rules."""
    duck_db = settings.EXPORT_DUCK_DB if duck_db is None else duck_db
    quip_lufs = settings.EXPORT_QUIP_LUFS if quip_lufs is None else quip_lufs
    spoken = [q for q in sorted(quips, key=lambda q: q.timestamp) if q.audio_path and q.duration_sec]
    video_filters: Optional[List[str]]
    if vertical:  # `duration` is the output length; vertical is vertical_crop's window and 9:16 size (even)
        x, y, crop_w, crop_h, out_w, out_h = vertical
        if fit:  # the whole frame over a blurred, darkened copy of itself that fills the 9:16 canvas (blurred small, then enlarged: cheap)
            sw, sh = _even(out_w / 4), _even(out_h / 4)
            video_filters = None
            front = f"[fg]scale={out_w}:-2:flags=lanczos[fgs]"
            back = (
                f"[bg]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},boxblur=6:2,"
                f"scale={out_w}:{out_h}:flags=bicubic,eq=brightness=-0.12[bgs]"
            )
        else:
            pos = f"x='{pan_x}':y={y}" if pan_x is not None else f"{x}:{y}"
            video_filters = [f"crop={crop_w}:{crop_h}:{pos}", f"scale={out_w}:{out_h}:flags=lanczos"]
    else:
        # even dimensions are required by yuv420p H.264; this changes nothing for even-sized video
        video_filters = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    if video_filters is None:
        tail = f";[bgs][fgs]overlay=(W-w)/2:(H-h)/2" + (f",subtitles={captions_file}" if captions_file else "") + "[vout]"
        graph = ["[0:v]split=2[bg][fg]", back, front + tail]
    else:
        if captions_file:
            video_filters.append(f"subtitles={captions_file}")
        graph = [f"[0:v]{','.join(video_filters)}[vout]"]

    if spoken:
        if has_audio:
            ducks = duck_intervals(spoken)
            enable = "+".join(f"between(t,{_num(a)},{_num(b)})" for a, b in ducks)
            graph.append(f"[0:a]volume={_num(duck_db)}dB:enable='{enable}'[base]")
        else:
            graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{_num(duration)}[base]")
        labels = ["[base]"]
        for i, quip in enumerate(spoken, start=1):
            delay_ms = max(0, round(quip.timestamp * 1000))
            graph.append(
                f"[{i}:a]{QUIP_COMPRESSOR},loudnorm=I={_num(quip_lufs)}:TP=-1.5:LRA=7,aresample=48000,adelay={delay_ms}:all=1[q{i}]"
            )
            labels.append(f"[q{i}]")
        graph.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0:dropout_transition=0,"
            f"alimiter=limit=0.95[aout]"
        )
    return ";\n".join(graph)


def build_ffmpeg_args(
    ffmpeg: str,
    video_path: str,
    output_name: str,
    duration: float,
    has_audio: bool,
    quips: List[ExportQuip],
    graph_file: str = GRAPH_FILE,
    start: Optional[float] = None,
) -> List[str]:
    """The ffmpeg command line. The filter graph is read from `graph_file` (see build_filter_graph) because a long graph
    would exceed the Windows command-line limit; `graph_file` and `output_name` are relative to ffmpeg's working directory."""
    spoken = [q for q in sorted(quips, key=lambda q: q.timestamp) if q.audio_path and q.duration_sec]
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    if start:  # input seek (frame-accurate: the video is re-encoded), so audio and quip timestamps begin at 0
        args += ["-ss", _num(start), "-t", _num(duration)]
    args += ["-i", video_path]
    for quip in spoken:
        args += ["-i", quip.audio_path]
    args += ["-filter_complex_script", graph_file, "-map", "[vout]"]
    if spoken:
        args += ["-map", "[aout]"]
    elif has_audio:
        args += ["-map", "0:a:0"]
    args += ["-c:v", "libx264", "-crf", str(CRF), "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if spoken or has_audio:
        args += ["-c:a", "aac", "-b:a", "192k"]
    args += ["-t", _num(duration), "-movflags", "+faststart", output_name]
    return args


# ---- rendering -------------------------------------------------------------------------------


def export_dir(job_id: str) -> str:
    return os.path.join(settings.UPLOAD_DIR, "exports", job_id)


def export_name(captions: bool, aspect: str = "original", start: Optional[float] = None, end: Optional[float] = None) -> str:
    """The cache key as a file name: captions, aspect and the normalized range. Untouched defaults keep the old
    `captions<0|1>.mp4`, so exports rendered before these options existed stay valid."""
    name = f"captions{int(captions)}"
    if aspect != "original":
        name += "_" + aspect.replace(":", "x")
    if start is not None:
        name += f"_s{_num(start)}"
    if end is not None:
        name += f"_e{_num(end)}"
    return name + ".mp4"


def export_file(job_id: str, captions: bool, aspect: str = "original", start: Optional[float] = None, end: Optional[float] = None) -> str:
    return os.path.join(export_dir(job_id), export_name(captions, aspect, start, end))


def _probe_video(path: str) -> Tuple[int, int, float]:
    cap = cv2.VideoCapture(path)
    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    finally:
        cap.release()
    if width <= 0 or height <= 0 or fps <= 0 or frames <= 0:
        raise ExportError("The source video could not be read.")
    return width, height, frames / fps


async def _run(args: List[str], cwd: str, timeout: float) -> Tuple[int, str]:
    """Run a process, return (exit code, stderr). The process is killed on timeout or cancellation."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except NotImplementedError:  # an event loop without subprocess support (Windows selector loop)
        done = await asyncio.to_thread(
            subprocess.run, args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
        return done.returncode, done.stderr.decode("utf-8", "replace")
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stderr.decode("utf-8", "replace")


async def _has_audio_stream(ffmpeg: str, video_path: str, cwd: str) -> bool:
    _, stderr = await _run([ffmpeg, "-hide_banner", "-nostdin", "-i", video_path], cwd, timeout=60)
    return re.search(r"Stream #\d+:\d+.*: Audio:", stderr) is not None


def export_quips(job_id: str, quips: List[Dict[str, Any]]) -> List[ExportQuip]:
    """Pair each stored quip with the mp3 on disk. A quip that claims audio whose file is gone is an error, not silence."""
    result = []
    for quip in quips:
        audio_path = None
        if quip.get("audio_url") and quip.get("duration_sec"):
            audio_path = tts_service.clip_path(job_id, quip["audio_url"])
            if audio_path is None or not os.path.isfile(audio_path):
                raise ExportError("The commentary audio is no longer available. Process the clip again.", 404)
        result.append(ExportQuip(float(quip["timestamp"]), str(quip["text"]), audio_path, quip.get("duration_sec")))
    return result


def _relative_audio(quips: List[ExportQuip], folder: str) -> List[ExportQuip]:
    """Quip audio paths relative to ffmpeg's working directory: hundreds of absolute -i paths would overflow the
    Windows command line. A path on another drive stays absolute."""
    result = []
    for quip in quips:
        path = quip.audio_path
        if path:
            try:
                path = os.path.relpath(path, folder)
            except ValueError:
                pass
        result.append(ExportQuip(quip.timestamp, quip.text, path, quip.duration_sec))
    return result


_locks: Dict[Tuple, asyncio.Lock] = {}


def check_options(aspect: str, start: Optional[float], end: Optional[float]) -> None:
    """The checks that need no video: an unknown aspect, a negative start, start not before end. Raises ExportError (400)."""
    if aspect not in ASPECTS:
        raise ExportError(f"Unknown aspect '{aspect}'. Use one of: {', '.join(ASPECTS)}.", 400)
    for value in (start, end):
        if value is not None and not math.isfinite(value):
            raise ExportError("The start and end times must be numbers of seconds.", 400)
    if start is not None and start < 0:
        raise ExportError("The start time cannot be negative.", 400)
    if start is not None and end is not None and start >= end:
        raise ExportError("The start time must be before the end time.", 400)


async def render_export(
    job_id: str, video_path: str, quips: List[Dict[str, Any]], captions: bool,
    aspect: str = "original", start: Optional[float] = None, end: Optional[float] = None, framing: str = "auto",
) -> str:
    """Return the path of the rendered mp4, rendering it first if it is not cached. `aspect` is "original" or "9:16"
    (framed automatically, see plan_vertical); `start`/`end` trim to that range in seconds. `framing` is for dev scripts
    only ("auto", "static", "crop", "fit"; the API always uses "auto") and is not part of the cache name. Raises ExportError."""
    check_options(aspect, start, end)
    untouched = aspect == "original" and start is None and end is None
    final = export_file(job_id, captions)
    lock = _locks.setdefault((job_id, captions, aspect, start, end), asyncio.Lock())
    async with lock:
        if untouched and os.path.isfile(final):
            return final
        if not os.path.isfile(video_path):
            raise ExportError("The source video is no longer available. Process the clip again.", 404)
        width, height, duration = await asyncio.to_thread(_probe_video, video_path)
        start, end = normalize_range(start, end, duration)
        final = export_file(job_id, captions, aspect, start, end)
        if os.path.isfile(final):
            return final
        ffmpeg = resolve_ffmpeg()
        items = select_trim_quips(export_quips(job_id, quips), start, end)
        folder = export_dir(job_id)
        os.makedirs(folder, exist_ok=True)
        partial = os.path.basename(final)[: -len(".mp4")] + ".partial.mp4"
        length = (end if end is not None else duration) - (start or 0.0)
        try:
            has_audio = await _has_audio_stream(ffmpeg, video_path, folder)
            vertical, pan_x, fit, zone_for = None, None, False, None
            out_w, out_h = width, height
            if aspect == "9:16":
                vertical, pan_x, fit, zone_for = await asyncio.to_thread(
                    plan_vertical, video_path, width, height, duration, start or 0.0, length, framing,
                )
                out_w, out_h = vertical[4], vertical[5]
            captions_file = None
            if captions:
                captions_file = CAPTIONS_FILE
                with open(os.path.join(folder, captions_file), "w", encoding="utf-8") as f:
                    f.write(build_ass(items, out_w, out_h, vertical=vertical is not None, zone_for=zone_for))
            with open(os.path.join(folder, GRAPH_FILE), "w", encoding="utf-8") as f:
                f.write(build_filter_graph(length, has_audio, items, captions_file, vertical=vertical, pan_x=pan_x, fit=fit))
            args = build_ffmpeg_args(
                ffmpeg, video_path, partial, length, has_audio, _relative_audio(items, folder), start=start,
            )
            try:
                code, stderr = await _run(args, folder, settings.EXPORT_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                logger.error("ffmpeg export for job %s timed out after %ss", job_id, settings.EXPORT_TIMEOUT_SECONDS)
                raise ExportError("Rendering the video took too long.")
            except OSError:
                logger.exception("Could not start ffmpeg for job %s", job_id)
                raise ExportError("ffmpeg could not be started.")
            if code != 0 or not os.path.isfile(os.path.join(folder, partial)):
                logger.error("ffmpeg exited with %s for job %s: %s", code, job_id, stderr[-STDERR_TAIL:])
                raise ExportError("Rendering the video failed.")
            os.replace(os.path.join(folder, partial), final)
        except BaseException:
            try:
                os.remove(os.path.join(folder, partial))
            except OSError:
                pass
            raise
        return final


def forget_job_locks(job_id: str) -> None:
    for key in [k for k in _locks if k[0] == job_id]:
        _locks.pop(key, None)
