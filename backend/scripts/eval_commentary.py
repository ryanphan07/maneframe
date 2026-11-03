"""Compare commentary prompts and styles on a fixed set of clips. Runs the LLM step only (no TTS, no job store).

Run from backend/ with the venv active and llama-server up:
    python scripts/eval_commentary.py --styles hype,deadpan
Clips are listed one path per line in eval/clips.txt (blank lines and # comments are ignored). Each run writes
eval/results/<timestamp>.csv with one row per quip: clip, window, t, style, prompt_hash, text, rating (blank).
Fill in the rating column (any number, e.g. 1-5) by hand, then:
    python scripts/eval_commentary.py --summarize [file.csv ...]
prints the mean rating per prompt_hash and style over the rated rows (all of eval/results when no file is given).
prompt_hash identifies the full system prompt (base, glossary, examples, style), so editing a prompt file starts a new group.
"""
import argparse
import asyncio
import csv
import datetime
import sys
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
        with open(path, newline="", encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    return rows


async def eval_clip(clip: str, style: str, prompt_hash: str, max_windows: Optional[int]) -> List[Dict[str, object]]:
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
        try:
            result = await commentary._analyze_with_retry(path, start, end, box, context, label, style)
        except Exception as exc:
            if commentary.is_unreachable(exc):
                raise
            print(f"  {label} failed ({type(exc).__name__}: {exc}); no rows for it", file=sys.stderr)
            continue
        context.update(result)
        for quip in result.quips:
            rows.append({
                "clip": Path(clip).name, "window": i + 1, "t": round(quip["timestamp"], 2), "style": style,
                "prompt_hash": prompt_hash, "text": quip["text"], "rating": "",
            })
    return rows


async def run_eval(clips: List[str], styles: List[str], max_windows: Optional[int], out_dir: Path) -> Path:
    from app.services import llm

    known = llm.list_styles()
    for style in styles:
        if style not in known:
            raise SystemExit(f"Unknown style {style!r}. Choose from: {', '.join(known)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.csv"
    total = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for clip in clips:
            for style in styles:
                rows = await eval_clip(clip, style, llm.prompt_hash(style), max_windows)
                writer.writerows(rows)
                f.flush()  # a long run that dies late keeps what it already got
                total += len(rows)
    print(f"Wrote {total} quips to {out}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clips", type=Path, default=CLIPS_FILE, help="text file with one clip path per line")
    parser.add_argument("--clip", action="append", help="a clip path to use instead of the clips file (repeatable)")
    parser.add_argument("--styles", default="hype", help="comma-separated styles (default: hype)")
    parser.add_argument("--max-windows", type=int, help="only the first N windows of each clip")
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
    styles = [s.strip() for s in args.styles.split(",") if s.strip()]
    asyncio.run(run_eval(clips, styles, args.max_windows, args.out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
