"""Check whether each quip in an eval CSV is grounded in the video: is what the line claims visible in the frames?

There is no vision judge in the app, so a human (or the reviewing session) judges. This script prepares the review and
tallies the result.

    python scripts/grounding_check.py extract eval/results/<run>.csv --clip-dir <folder with the clips>
writes one image per quip to logs/grounding/<run>/ (the frames at t-1.5 s, t and t+1.5 s side by side, cropped like the
model saw them) and logs/grounding/<run>.csv, a review sheet: the eval rows plus `image`, `grounded` (yes / no / unclear)
and `grounded_reason`. Fill in the last two columns, then:
    python scripts/grounding_check.py summarize logs/grounding/<run>.csv [...]
prints the grounded rate per prompt_hash and style (unclear counts as not grounded; the yes/no/unclear split is shown).
"""
import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

OUT_DIR = REPO_DIR / "logs" / "grounding"
SHEET_EXTRA = ["image", "grounded", "grounded_reason"]
VERDICTS = ("yes", "no", "unclear")
OFFSETS = (-1.5, 0.0, 1.5)


def strip_times(t: float, duration: float, offsets: Tuple[float, ...] = OFFSETS) -> List[float]:
    """Sample times around t, clamped into the clip."""
    return [min(max(t + o, 0.0), max(duration - 0.05, 0.0)) for o in offsets]


def normalize_verdict(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip().lower()
    return v if v in VERDICTS else None


def tally(rows: Iterable[Dict[str, str]]) -> List[Dict[str, object]]:
    """Per (prompt_hash, style): counts of yes / no / unclear and the grounded rate (yes over judged rows)."""
    counts: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: {v: 0 for v in VERDICTS})
    for row in rows:
        v = normalize_verdict(row.get("grounded"))
        if v:
            counts[(row.get("prompt_hash", ""), row.get("style", ""))][v] += 1
    out = []
    for (h, s), c in sorted(counts.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        n = sum(c.values())
        out.append({"prompt_hash": h, "style": s, "n": n, **c, "rate": c["yes"] / n})
    return out


def format_tally(groups: List[Dict[str, object]]) -> str:
    if not groups:
        return "No judged rows found. Fill in the grounded column of a review sheet first."
    lines = [f"{'prompt_hash':<12} {'style':<10} {'n':>3} {'yes':>4} {'no':>4} {'unclear':>8} {'rate':>6}"]
    lines += [
        f"{g['prompt_hash']:<12} {g['style']:<10} {g['n']:>3} {g['yes']:>4} {g['no']:>4} {g['unclear']:>8} {g['rate']:>6.0%}"
        for g in groups
    ]
    return "\n".join(lines)


def extract(csv_path: Path, clip_dir: Path, out_root: Path) -> Path:
    import cv2
    import numpy as np

    from app.services import video_processor as vp

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    img_dir = out_root / csv_path.stem
    img_dir.mkdir(parents=True, exist_ok=True)
    per_clip: Dict[str, Tuple[str, float, object]] = {}
    for clip in sorted({r["clip"] for r in rows}):
        path = clip_dir / clip
        if not path.exists():
            raise SystemExit(f"Clip not found: {path} (use --clip-dir)")
        duration = vp.get_video_duration(str(path))
        per_clip[clip] = (str(path), duration, vp.compute_crop_box(str(path), duration))
    sheet = out_root / f"{csv_path.stem}.csv"
    fields = list(rows[0].keys()) if rows else []
    fields += [c for c in SHEET_EXTRA if c not in fields]
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for i, row in enumerate(rows, 1):
            path, duration, box = per_clip[row["clip"]]
            t = float(row["t"])
            cap = cv2.VideoCapture(path)
            tiles = []
            try:
                for st in strip_times(t, duration):
                    cap.set(cv2.CAP_PROP_POS_MSEC, st * 1000)
                    ok, frame = cap.read()
                    if not ok:
                        continue
                    if box:
                        h, w = frame.shape[:2]
                        x0, y0, x1, y1 = box
                        frame = frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
                    fh, fw = frame.shape[:2]
                    frame = cv2.resize(frame, (480, max(1, int(fh * 480 / fw))), interpolation=cv2.INTER_AREA)
                    cv2.putText(frame, f"{st:.1f}s", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
                    tiles.append(frame)
            finally:
                cap.release()
            name = f"{i:03d}_{row['style']}_{row['prompt_hash']}_t{t:.1f}.jpg"
            if tiles:
                height = min(t_.shape[0] for t_ in tiles)
                cv2.imwrite(str(img_dir / name), np.hstack([t_[:height] for t_ in tiles]), [cv2.IMWRITE_JPEG_QUALITY, 85])
            writer.writerow({**row, "image": f"{csv_path.stem}/{name}" if tiles else ""})
    print(f"Wrote {len(rows)} images to {img_dir} and the review sheet {sheet}")
    return sheet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract", help="write the frame strips and a review sheet for an eval CSV")
    ex.add_argument("csv", type=Path)
    ex.add_argument("--clip-dir", type=Path, required=True, help="folder that holds the clips named in the CSV")
    ex.add_argument("--out-dir", type=Path, default=OUT_DIR)
    su = sub.add_parser("summarize", help="grounded rate per prompt_hash and style")
    su.add_argument("sheets", nargs="+", type=Path)
    args = parser.parse_args()
    if args.cmd == "extract":
        extract(args.csv, args.clip_dir, args.out_dir)
    else:
        rows: List[Dict[str, str]] = []
        for p in args.sheets:
            with open(p, newline="", encoding="utf-8") as f:
                rows.extend(csv.DictReader(f))
        print(format_tally(tally(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
