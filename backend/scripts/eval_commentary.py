"""Compare commentary prompts and styles on a fixed set of clips. Runs the LLM step only (no TTS, no job store).

Run from backend/ with the venv active and llama-server up:
    python scripts/eval_commentary.py --styles hype,deadpan
Clips are listed one path per line in eval/clips.txt (blank lines and # comments are ignored). Each run writes
eval/results/<timestamp>.csv with one row per quip: clip, window, t, style, prompt_hash, text, rating (blank).
Pass --prepared-dir <folder> to run the clips through the app's prepare step first (needed for HEVC/HDR phone clips).
Fill in the rating column (any number, e.g. 1-5) by hand, then:
    python scripts/eval_commentary.py --summarize [file.csv ...]
prints the mean rating per prompt_hash and style over the rated rows (all of eval/results when no file is given).
prompt_hash identifies the full system prompt (base, glossary, examples, style), so editing a prompt file starts a new group.
"""
import argparse
import asyncio
import csv
import datetime
import json
import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

CLIPS_FILE = REPO_DIR / "eval" / "clips.txt"
RESULTS_DIR = REPO_DIR / "eval" / "results"
FIELDS = ["clip", "window", "t", "style", "prompt_hash", "text", "rating"]


def parse_clip_list(text: str) -> List[str]:
    """Clip paths from clips.txt: one per line, blank lines and lines starting with # skipped."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def parse_rating(value: Optional[str]) -> Optional[float]:
    """The rating as a number, or None when the cell is blank or not a number."""
    try:
        return float((value or "").strip())
    except ValueError:
        return None


def summarize(rows: Iterable[Dict[str, str]]) -> List[Dict[str, object]]:
    """Mean rating and rated-row count per (prompt_hash, style), sorted by style then best mean first. Unrated rows are ignored."""
    ratings: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for row in rows:
        rating = parse_rating(row.get("rating"))
        if rating is not None:
            ratings[(row.get("prompt_hash", ""), row.get("style", ""))].append(rating)
    groups = [
        {"prompt_hash": h, "style": s, "n": len(r), "mean": sum(r) / len(r)} for (h, s), r in ratings.items()
    ]
    return sorted(groups, key=lambda g: (g["style"], -g["mean"], g["prompt_hash"]))


def format_summary(groups: List[Dict[str, object]]) -> str:
    if not groups:
        return "No rated rows found. Fill in the rating column of an eval CSV first."
    lines = [f"{'prompt_hash':<12} {'style':<10} {'rated':>5} {'mean':>6}"]
    lines += [f"{g['prompt_hash']:<12} {g['style']:<10} {g['n']:>5} {g['mean']:>6.2f}" for g in groups]
    return "\n".join(lines)


def read_rows(paths: List[Path]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows.extend(csv.DictReader(f))
    return rows


async def prepare_clip(clip: str, prepared_dir: Path) -> str:
    """A job-ready copy of the clip (upright SDR H.264, as the app makes it) in prepared_dir, cached by file name.
    The eval then sees the same frames a real job does, not raw HDR HEVC. Delete the cached file to redo it."""
    import shutil

    from app.services.pipeline import prepare_video

    target = prepared_dir / Path(clip).name
    if target.exists():
        return str(target)
    prepared_dir.mkdir(parents=True, exist_ok=True)
    partial = prepared_dir / (Path(clip).name + ".part")
    await asyncio.to_thread(shutil.copyfile, clip, partial)

    async def quiet(*_args) -> None:
        return None

    try:
        await prepare_video(str(partial), quiet)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return str(target)


FACTS_PROMPT = (
    "You inspect frames of a lion dance. Three lions may be on stage (blue, yellow, pink); drummers sit at one side. "
    "Return one entry per frame, in the order shown: each lion you can see as color + horizontal position in the frame "
    "(left, center, right) + pose (crouched: low to the ground; standing: upright on all legs; reared_up: head lifted "
    "high on the tail dancer; leaning), and whether drummers are visible. An empty stage has an empty lions list. "
    "Report only what the pixels show; if unsure of a pose use unclear. No commentary."
)
LION_FACTS = [f"{c} {pos} {pose}" for c in ("blue", "yellow", "pink", "other") for pos in ("left", "center", "right")
              for pose in ("crouched", "standing", "reared_up", "leaning", "unclear")]
FACTS_SCHEMA = {  # compact on purpose: output tokens, not the images, dominate the facts call's time
    "type": "json_schema",
    "json_schema": {
        "name": "facts",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"frames": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "lions": {"type": "array", "items": {"type": "string", "enum": LION_FACTS}},
                    "drummers": {"type": "boolean"},
                },
                "required": ["lions", "drummers"],
                "additionalProperties": False,
            }}},
            "required": ["frames"],
            "additionalProperties": False,
        },
    },
}

STATS_LOG: List[Dict[str, object]] = []  # per-call usage collected during a run (written next to the CSV)


def format_facts(data: Dict[str, object], times: List[float]) -> str:
    """The facts reply (one entry per frame, in order) as one text block for the commentary call."""
    lines = []
    for t, fr in zip(times, data.get("frames", [])):
        lions = "; ".join(f"{l.replace('_', ' ')}" for l in fr.get("lions", [])) or "no lions on stage"
        lines.append(f"t={t}s: {lions}; " + ("drummers visible" if fr.get("drummers") else "no drummers visible"))
    return (
        "Facts about each frame (lion color, position in frame, pose) from a separate careful look (trust them over "
        "your own first impression, and do not claim anything about poses or positions that contradicts them):\n"
        + "\n".join(lines) + "\n"
    )


async def get_facts(frames: List[Dict[str, object]], start: float, end: float) -> Dict[str, object]:
    """Eval-only extra call: schema-constrained per-frame facts (lion color, position, pose; drummers)."""
    from app.config import settings
    from app.services import llm

    content: List[Dict[str, object]] = [{"type": "text", "text": f"These frames cover {start:.1f}s to {end:.1f}s."}]
    for frame in frames:
        content.append({"type": "text", "text": f"Frame at t={frame['timestamp']}s:"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame['image_data']}"}})
    began = time.monotonic()
    response = await llm.llm_client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=[{"role": "system", "content": FACTS_PROMPT}, {"role": "user", "content": content}],
        temperature=0.2,
        max_tokens=500,
        response_format=FACTS_SCHEMA,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    usage = getattr(response, "usage", None)
    STATS_LOG.append({
        "call": "facts", "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None), "seconds": round(time.monotonic() - began, 1),
    })
    return json.loads(response.choices[0].message.content or "{}")


class UsageHandler(logging.Handler):
    """Collects the 'LLM <label>: frames=.. prompt_tokens=.. completion_tokens=.. seconds=..' lines llm._call logs."""

    def emit(self, record: logging.LogRecord) -> None:
        m = re.search(r"frames=(\d+) prompt_tokens=(\S+) completion_tokens=(\S+) seconds=([\d.]+)", record.getMessage())
        if m:
            STATS_LOG.append({"call": "commentary", "frames": int(m[1]), "prompt_tokens": m[2], "completion_tokens": m[3], "seconds": float(m[4])})


def apply_rules_file(path: Path) -> None:
    """Eval-only: append the rules in `path` to every style's text, so prompt_hash reflects them."""
    from functools import lru_cache

    from app.services import llm

    extra = path.read_text(encoding="utf-8").strip()
    original = llm._read_style.__wrapped__

    @lru_cache(maxsize=None)
    def patched(style: str) -> str:
        return original(style) + "\n" + extra

    llm._read_style = patched


async def analyze_with_facts(path: str, start: float, end: float, box: object, context: object, label: str, style: str):
    """Two-step window: a facts call, then the usual commentary call with the facts in its message (one retry)."""
    from app.config import settings
    from app.services import commentary, llm
    from app.services import video_processor as vp

    for attempt in range(2):
        frames = await asyncio.to_thread(vp.sample_window_frames, path, start, end, settings.FRAMES_PER_WINDOW, settings.FRAME_WIDTH, box)
        if not frames:
            raise ValueError(f"No frames could be extracted for {label}")
        try:
            facts = format_facts(await get_facts(frames, start, end), [f['timestamp'] for f in frames])
            print("    " + facts.splitlines()[1][:150], flush=True)
            return await llm.analyze_window(frames, start, end, context.quips, label=label, earlier_seen=context.seen, style=style, facts=facts)
        except llm.LLMOutputError:
            raise
        except Exception as exc:
            if attempt or commentary.is_unreachable(exc):
                raise


async def eval_clip(clip: str, style: str, prompt_hash: str, max_windows: Optional[int], use_facts: bool = False, window_log: Optional[List[Dict[str, object]]] = None) -> List[Dict[str, object]]:
    """The LLM step of the pipeline for one clip and style (same windows, frames and continuity), one row per quip."""
    from app.config import settings
    from app.services import commentary
    from app.services import video_processor as vp

    path = str(Path(clip))
    duration = await asyncio.to_thread(vp.get_video_duration, path)
    box = await asyncio.to_thread(vp.compute_crop_box, path, duration)
    windows = vp.plan_windows(duration, settings.WINDOW_SECONDS)
    if max_windows:
        windows = windows[:max_windows]
    context = commentary.Continuity()
    rows: List[Dict[str, object]] = []
    for i, (start, end) in enumerate(windows):
        label = f"window {i + 1}/{len(windows)}"
        print(f"  {Path(clip).name} [{style}] {label}", flush=True)
        n_calls = len(STATS_LOG)
        began = time.monotonic()
        try:
            if use_facts:
                result = await analyze_with_facts(path, start, end, box, context, label, style)
            else:
                result = await commentary.analyze_window_with_retry(path, start, end, box, context, label, style)
        except Exception as exc:
            if commentary.is_unreachable(exc):
                raise
            print(f"  {label} failed ({type(exc).__name__}: {exc}); no rows for it", file=sys.stderr)
            continue
        if window_log is not None:
            calls = STATS_LOG[n_calls:]
            window_log.append({
                "clip": Path(clip).name, "window": i + 1, "seconds": round(time.monotonic() - began, 1),
                "prompt_tokens": [c["prompt_tokens"] for c in calls], "calls": len(calls),
            })
        context.update(result)
        for quip in result.quips:
            rows.append({
                "clip": Path(clip).name, "window": i + 1, "t": round(quip["timestamp"], 2), "style": style,
                "prompt_hash": prompt_hash, "text": quip["text"], "rating": "",
            })
    return rows


async def run_eval(clips: List[str], styles: List[str], max_windows: Optional[int], out_dir: Path, prepared_dir: Optional[Path] = None, use_facts: bool = False) -> Path:
    from app.services import llm

    known = llm.list_styles()
    for style in styles:
        if style not in known:
            raise SystemExit(f"Unknown style {style!r}. Choose from: {', '.join(known)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.csv"
    total = 0
    window_log: List[Dict[str, object]] = []
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for clip in clips:
            if prepared_dir is not None:
                print(f"  preparing {Path(clip).name}", flush=True)
                clip = await prepare_clip(clip, prepared_dir)
            for style in styles:
                rows = await eval_clip(clip, style, llm.prompt_hash(style), max_windows, use_facts, window_log)
                writer.writerows(rows)
                f.flush()  # a long run that dies late keeps what it already got
                total += len(rows)
    from app.config import settings

    secs = [w["seconds"] for w in window_log]
    stats = {
        "frames_per_window": settings.FRAMES_PER_WINDOW, "frame_width": settings.FRAME_WIDTH, "facts": use_facts,
        "seconds_per_window": round(sum(secs) / len(secs), 1) if secs else None, "windows": window_log, "calls": STATS_LOG,
    }
    out.with_suffix(".stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print(f"Wrote {total} quips to {out}; {stats['seconds_per_window']} s per window")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clips", type=Path, default=CLIPS_FILE, help="text file with one clip path per line")
    parser.add_argument("--clip", action="append", help="a clip path to use instead of the clips file (repeatable)")
    parser.add_argument("--styles", default="hype", help="comma-separated styles (default: hype)")
    parser.add_argument("--max-windows", type=int, help="only the first N windows of each clip")
    parser.add_argument("--prepared-dir", type=Path, help="convert each clip like a job does (HEVC/HDR/rotation to upright SDR H.264) into this folder first and evaluate that copy; cached by file name")
    parser.add_argument("--rules-file", type=Path, help="eval only: append these lines to the style text (changes prompt_hash)")
    parser.add_argument("--frames", type=int, help="eval only: FRAMES_PER_WINDOW for this run")
    parser.add_argument("--frame-width", type=int, help="eval only: FRAME_WIDTH for this run")
    parser.add_argument("--facts", action="store_true", help="eval only: two-step windows (a schema-constrained facts call feeds the commentary call)")
    parser.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--summarize", nargs="*", type=Path, metavar="CSV", help="print mean rating per prompt_hash and style")
    args = parser.parse_args()

    if args.summarize is not None:
        paths = args.summarize or sorted(args.out_dir.glob("*.csv"))
        print(format_summary(summarize(read_rows(paths))))
        return 0

    clips = args.clip or (parse_clip_list(args.clips.read_text(encoding="utf-8")) if args.clips.exists() else [])
    if not clips:
        print(f"No clips: list paths in {args.clips} or pass --clip", file=sys.stderr)
        return 2
    missing = [c for c in clips if not Path(c).exists()]
    if missing:
        print(f"Clip not found: {missing[0]}", file=sys.stderr)
        return 2
    from app.config import settings

    if args.frames:
        settings.FRAMES_PER_WINDOW = args.frames
    if args.frame_width:
        settings.FRAME_WIDTH = args.frame_width
    if args.rules_file:
        apply_rules_file(args.rules_file)
    logging.getLogger("maneframe.llm").addHandler(UsageHandler())
    logging.getLogger("maneframe.llm").setLevel(logging.INFO)
    styles = [s.strip() for s in args.styles.split(",") if s.strip()]
    asyncio.run(run_eval(clips, styles, args.max_windows, args.out_dir, args.prepared_dir, args.facts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
