"""Render a finished job as a shareable mp4: original video, original audio ducked under the quips, quip audio at its
scheduled timestamps, and optionally the quip text burned in as captions."""

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import cv2

from app.config import settings
from app.services import tts as tts_service

logger = logging.getLogger("maneframe.export")

DUCK_MERGE_GAP_SEC = 0.5  # quips closer than this share one ducked stretch instead of pumping the volume
NO_AUDIO_CAPTION_SEC = 3.0
CRF = 23
CAPTIONS_FILE = "captions.ass"
GRAPH_FILE = "filter_graph.txt"
STDERR_TAIL = 2000


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


def build_ass(quips: List[ExportQuip], width: int, height: int) -> str:
    """An ASS subtitle file sized to the video: bottom center, white text on a translucent box."""
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
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}")
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


def build_filter_graph(
    duration: float,
    has_audio: bool,
    quips: List[ExportQuip],
    captions_file: Optional[str] = None,
    duck_db: Optional[float] = None,
    quip_lufs: Optional[float] = None,
) -> str:
    """The -filter_complex graph. Each quip is loudness-normalized to `quip_lufs` (loudnorm) so both TTS engines land at
    the same level, and the original is lowered by `duck_db` while a quip plays; both default to the settings. `captions_file` is relative: ffmpeg runs with the job's export folder as its working
    directory, which keeps drive letters out of the subtitles filter's escaping rules."""
    duck_db = settings.EXPORT_DUCK_DB if duck_db is None else duck_db
    quip_lufs = settings.EXPORT_QUIP_LUFS if quip_lufs is None else quip_lufs
    spoken = [q for q in sorted(quips, key=lambda q: q.timestamp) if q.audio_path and q.duration_sec]
    # even dimensions are required by yuv420p H.264; this changes nothing for even-sized video
    video_filters = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]
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
                f"[{i}:a]loudnorm=I={_num(quip_lufs)}:TP=-1.5:LRA=7,aresample=48000,adelay={delay_ms}:all=1[q{i}]"
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
) -> List[str]:
    """The ffmpeg command line. The filter graph is read from `graph_file` (see build_filter_graph) because a long graph
    would exceed the Windows command-line limit; `graph_file` and `output_name` are relative to ffmpeg's working directory."""
    spoken = [q for q in sorted(quips, key=lambda q: q.timestamp) if q.audio_path and q.duration_sec]
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", video_path]
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


def export_file(job_id: str, captions: bool) -> str:
    return os.path.join(export_dir(job_id), f"captions{int(captions)}.mp4")


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


_locks: Dict[Tuple[str, bool], asyncio.Lock] = {}


async def render_export(job_id: str, video_path: str, quips: List[Dict[str, Any]], captions: bool) -> str:
    """Return the path of the rendered mp4, rendering it first if it is not cached. Raises ExportError."""
    final = export_file(job_id, captions)
    lock = _locks.setdefault((job_id, captions), asyncio.Lock())
    async with lock:
        if os.path.isfile(final):
            return final
        if not os.path.isfile(video_path):
            raise ExportError("The source video is no longer available. Process the clip again.", 404)
        ffmpeg = resolve_ffmpeg()
        items = export_quips(job_id, quips)
        folder = export_dir(job_id)
        os.makedirs(folder, exist_ok=True)
        partial = f"captions{int(captions)}.partial.mp4"
        try:
            width, height, duration = await asyncio.to_thread(_probe_video, video_path)
            has_audio = await _has_audio_stream(ffmpeg, video_path, folder)
            captions_file = None
            if captions:
                captions_file = CAPTIONS_FILE
                with open(os.path.join(folder, captions_file), "w", encoding="utf-8") as f:
                    f.write(build_ass(items, width, height))
            with open(os.path.join(folder, GRAPH_FILE), "w", encoding="utf-8") as f:
                f.write(build_filter_graph(duration, has_audio, items, captions_file))
            args = build_ffmpeg_args(ffmpeg, video_path, partial, duration, has_audio, _relative_audio(items, folder))
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
