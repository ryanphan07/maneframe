r"""Render a 9:16 export of a clip without a model job, to judge the framing. Captions come from an eval CSV (the
`clip,window,t,...,text` files written by eval_commentary.py) and have no audio, so each shows for 3 s.

Run from backend/:
    venv\Scripts\python scripts/render_vertical.py CLIP --out ..\logs\export_check\s51 --framing auto|static|crop|fit
        [--quips-csv ..\eval\results\s35\file.csv] [--prepared-dir DIR] [--frame-every 2]
Writes <out>/<clip stem>_<framing>.mp4 and, with --frame-every, <out>/<clip stem>_<framing>/tNNN.jpg (one frame every N s).
Prints the render time. --prepared-dir converts HEVC/HDR clips like a job does first (as eval_commentary.py does)."""
import argparse
import asyncio
import csv
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from app.config import settings  # noqa: E402
from app.services import export as export_service  # noqa: E402


def read_quips(csv_path: Path, clip_name: str):
    quips = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["clip"] == clip_name and row.get("text"):
                quips.append({"timestamp": float(row["t"]), "text": row["text"], "audio_url": None, "duration_sec": None})
    return sorted({q["timestamp"]: q for q in quips}.values(), key=lambda q: q["timestamp"])


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clip", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--framing", default="auto", choices=["auto", "static", "crop", "fit"])
    parser.add_argument("--quips-csv", type=Path)
    parser.add_argument("--prepared-dir", type=Path)
    parser.add_argument("--frame-every", type=float, default=0.0, help="also extract one frame every N seconds")
    args = parser.parse_args()

    video = str(args.clip)
    if args.prepared_dir:
        from eval_commentary import prepare_clip

        video = await prepare_clip(video, args.prepared_dir)
    quips = read_quips(args.quips_csv, args.clip.name) if args.quips_csv else []
    args.out.mkdir(parents=True, exist_ok=True)
    work = args.out / "_work"
    settings.UPLOAD_DIR = str(work)
    job_id = f"{args.clip.stem}_{args.framing}"
    shutil.rmtree(work / "exports" / job_id, ignore_errors=True)
    started = time.perf_counter()
    rendered = await export_service.render_export(job_id, video, quips, bool(quips), aspect="9:16", framing=args.framing)
    seconds = time.perf_counter() - started
    target = args.out / f"{job_id}.mp4"
    shutil.copyfile(rendered, target)
    shutil.rmtree(work, ignore_errors=True)
    print(f"{target.name}: {seconds:.1f} s render, {len(quips)} captions")
    if args.frame_every > 0:
        frames = args.out / job_id
        frames.mkdir(exist_ok=True)
        subprocess.run(
            [export_service.resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(target),
             "-vf", f"fps=1/{args.frame_every},scale=360:-2", "-q:v", "3", str(frames / "t%03d.jpg")], check=True,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
