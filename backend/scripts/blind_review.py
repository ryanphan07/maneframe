"""Blind grounding review across arms: pool lines from several eval CSVs into one shuffled sheet with the arm hidden.

    python scripts/blind_review.py make <pool> --clip-dir <folder> --arm base_a=eval/results/s36a/x.csv --arm r1=... [--sample base_a=30]
writes logs/grounding/blind/<pool>/: pages/page_NN.jpg (four lines per page: id, clip, t, the line, and the frame strip at
t-1.5 s, t, t+1.5 s), sheet.csv (id, clip, t, text; no arm, run, prompt hash or model) and key.csv (id -> arm, source csv,
prompt_hash, source row). Judge from the pages only, then write verdicts, one per line, to verdicts.txt in the pool folder:
    <id> yes|no|unclear [inv|pose|face|who|unread] [free-text reason]
(failure types: inv = invented event, prop or story; pose = pose, motion or direction wrong at t; face = face, head, ear
or gaze claim; who = wrong lion or time; unread = pose unreadable from stills). Then:
    python scripts/blind_review.py unblind <pool>
joins the verdicts with the key and prints, per arm, n, yes / no / unclear, the grounded rate with its Wilson 95% interval
and the failure-type counts. `--sample arm=N` puts only N random lines of that arm in the pool (a rubric-drift check).
"""
import argparse
import csv
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

BLIND_DIR = REPO_DIR / "logs" / "grounding" / "blind"
PER_PAGE = 4
FTYPES = ("inv", "pose", "face", "who", "unread")


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval for k successes in n trials."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (centre - half, centre + half)


def parse_verdicts(text: str) -> Dict[int, Tuple[str, str, str]]:
    """{id: (verdict, failure type or '', reason)} from verdict lines; blank lines and # comments are skipped."""
    out: Dict[int, Tuple[str, str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 3)
        i, verdict = int(parts[0]), parts[1].lower()
        if verdict not in ("yes", "no", "unclear"):
            raise ValueError(f"bad verdict in line: {line}")
        ftype = parts[2].lower() if len(parts) > 2 and parts[2].lower() in FTYPES else ""
        rest = parts[2:] if not ftype else parts[3:]
        out[i] = (verdict, ftype, " ".join(rest))
    return out


def make(pool: str, clip_dir: Path, arms: List[str], samples: Dict[str, int], seed: int) -> None:
    import cv2
    import numpy as np

    import grounding_check as gc

    rng = random.Random(seed)
    items: List[Dict[str, str]] = []
    for spec in arms:
        label, _, path = spec.partition("=")
        csv_path = Path(path)
        sheet = gc.OUT_DIR / f"{csv_path.stem}.csv"
        if not sheet.exists():
            gc.extract(csv_path, clip_dir, gc.OUT_DIR)
        with open(sheet, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if label in samples:
            rows = rng.sample(rows, min(samples[label], len(rows)))
        for r in rows:
            items.append({**r, "arm": label, "source": str(csv_path), "image_path": str(gc.OUT_DIR / r["image"]) if r.get("image") else ""})
    rng.shuffle(items)
    root = BLIND_DIR / pool
    (root / "pages").mkdir(parents=True, exist_ok=True)
    with open(root / "sheet.csv", "w", newline="", encoding="utf-8") as f, open(root / "key.csv", "w", newline="", encoding="utf-8") as k:
        w = csv.writer(f)
        kw = csv.writer(k)
        w.writerow(["id", "clip", "t", "text"])
        kw.writerow(["id", "arm", "source", "prompt_hash", "window"])
        for n, it in enumerate(items, 1):
            w.writerow([n, it["clip"], it["t"], it["text"]])
            kw.writerow([n, it["arm"], it["source"], it["prompt_hash"], it["window"]])
    for page, start in enumerate(range(0, len(items), PER_PAGE), 1):
        tiles = []
        for n, it in enumerate(items[start:start + PER_PAGE], start + 1):
            img = cv2.imread(it["image_path"]) if it["image_path"] else None
            if img is None:
                img = np.zeros((200, 1440, 3), np.uint8)
            img = cv2.resize(img, (1440, int(img.shape[0] * 1440 / img.shape[1])))
            bar = np.full((64, 1440, 3), 30, np.uint8)
            cv2.putText(bar, f"#{n}  {it['clip'].replace('hoco2026_', '').replace('.mp4', '')}  t={float(it['t']):.1f}s", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            text = it["text"].encode("ascii", "replace").decode()
            cv2.putText(bar, text, (8, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            tiles.append(np.vstack([bar, img]))
        cv2.imwrite(str(root / "pages" / f"page_{page:03d}.jpg"), np.vstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"{len(items)} lines pooled in {root} ({-(-len(items) // PER_PAGE)} pages); arm counts hidden in key.csv")


def unblind(pool: str) -> None:
    root = BLIND_DIR / pool
    verdicts = parse_verdicts((root / "verdicts.txt").read_text(encoding="utf-8"))
    with open(root / "key.csv", newline="", encoding="utf-8") as f:
        key = {int(r["id"]): r for r in csv.DictReader(f)}
    with open(root / "sheet.csv", newline="", encoding="utf-8") as f:
        sheet = {int(r["id"]): r for r in csv.DictReader(f)}
    missing = sorted(set(key) - set(verdicts))
    if missing:
        raise SystemExit(f"{len(missing)} lines have no verdict, first: {missing[:10]}")
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, r in key.items():
        groups[r["arm"]].append(i)
    out_rows = []
    print(f"{'arm':<10} {'n':>3} {'yes':>4} {'no':>4} {'unc':>4} {'grounded':>9} {'95% Wilson':>12}  failure types")
    for arm, ids in sorted(groups.items()):
        c = Counter(verdicts[i][0] for i in ids)
        n, yes = len(ids), c["yes"]
        lo, hi = wilson(yes, n)
        ft = Counter(verdicts[i][1] or "?" for i in ids if verdicts[i][0] != "yes")
        print(f"{arm:<10} {n:>3} {yes:>4} {c['no']:>4} {c['unclear']:>4} {yes / n:>8.0%} {lo:>7.0%}-{hi:.0%}  " + " ".join(f"{k}={v}" for k, v in sorted(ft.items())))
    for i in sorted(key):
        out_rows.append({**key[i], "clip": sheet[i]["clip"], "t": sheet[i]["t"], "text": sheet[i]["text"],
                         "verdict": verdicts[i][0], "ftype": verdicts[i][1], "reason": verdicts[i][2]})
    with open(root / "unblinded.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"Wrote {root / 'unblinded.csv'}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("pool")
    m.add_argument("--clip-dir", type=Path, required=True)
    m.add_argument("--arm", action="append", required=True, metavar="LABEL=CSV")
    m.add_argument("--sample", action="append", default=[], metavar="LABEL=N")
    m.add_argument("--seed", type=int, default=37)
    u = sub.add_parser("unblind")
    u.add_argument("pool")
    a = p.parse_args()
    if a.cmd == "make":
        make(a.pool, a.clip_dir, a.arm, {k: int(v) for k, v in (s.split("=") for s in a.sample)}, a.seed)
    else:
        unblind(a.pool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
