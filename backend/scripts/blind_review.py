"""Blind grounding review across arms: pool lines from several eval CSVs into one shuffled sheet with the arm hidden.

    python scripts/blind_review.py make <pool> --clip-dir <folder> --arm base_a=eval/results/s36a/x.csv --arm r1=... [--sample base_a=30]
writes logs/grounding/blind/<pool>/: pages/page_NN.jpg (four lines per page: id, clip, t, the line, and the frame strip at
t-1.5 s, t, t+1.5 s), sheet.csv (id, clip, t, text; no arm, run, prompt hash or model) and key.csv (id -> arm, source csv,
prompt_hash, source row). Judge from the pages only, then write verdicts, one per line, to verdicts.txt in the pool folder:
    <id> yes|no|unclear [inv|pose|face|who|unread|named] [nt|bo] [free-text reason]
(failure types: inv = invented event, prop or story; pose = pose, motion or direction wrong at t; face = face, head, ear
or gaze claim; who = wrong lion or time; unread = pose unreadable from stills; named = names a specific move or routine
part the strip does not show). nt / bo = notable yes / no: is this the moment worth saying (entrance, stunt, scroll, formation
change, exit) or true but not exciting. unblind also counts lines that claim a mistake (oops, lost, behind, forgot...). Then:
    python scripts/blind_review.py unblind <pool>
joins the verdicts with the key and prints, per arm, n, yes / no / unclear, the grounded rate with its Wilson 95% interval
and the failure-type counts. `--sample arm=N` puts only N random lines of that arm in the pool (a rubric-drift check).
"""
import argparse
import csv
import math
import random
import re
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
HUMOR_DIR = REPO_DIR / "logs" / "grounding" / "owner_humor"
FTYPES = ("inv", "pose", "face", "who", "unread", "named")
NOTABLE = {"nt": "yes", "bo": "no"}  # optional verdict-line token: nt = a moment worth saying, bo = true but not worth saying
MISTAKE_WORDS = re.compile(r"\b(oops\w*|oopsie|lost|behind|forgot|forgets|mistake|wrong|missed|misses|late|stuck|fumble\w*|stumble\w*)\b", re.I)


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval for k successes in n trials."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (centre - half, centre + half)


def parse_verdicts(text: str) -> Dict[int, Tuple[str, str, str, str]]:
    """{id: (verdict, failure type or '', reason, notable yes/no or '')} from verdict lines; blank lines and # comments are skipped.
    After the verdict come an optional failure type and an optional nt/bo token (either order), then the free-text reason."""
    out: Dict[int, Tuple[str, str, str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        i, verdict = int(parts[0]), parts[1].lower()
        if verdict not in ("yes", "no", "unclear"):
            raise ValueError(f"bad verdict in line: {line}")
        words = line.split()[2:]
        ftype = notable = ""
        while words and (words[0].lower() in FTYPES or words[0].lower() in NOTABLE):
            w = words.pop(0).lower()
            if w in FTYPES and not ftype:
                ftype = w
            elif w in NOTABLE and not notable:
                notable = NOTABLE[w]
            else:
                words.insert(0, w)
                break
        out[i] = (verdict, ftype, " ".join(words), notable)
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
        with open(sheet, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if label in samples:
            rows = rng.sample(rows, min(samples[label], len(rows)))
        for r in rows:
            items.append({**r, "arm": label, "source": str(csv_path), "image_path": str(gc.OUT_DIR / r["image"]) if r.get("image") else ""})
    rng.shuffle(items)
    root = BLIND_DIR / pool
    (root / "pages").mkdir(parents=True, exist_ok=True)
    with open(root / "sheet.csv", "w", newline="", encoding="utf-8-sig") as f, open(root / "key.csv", "w", newline="", encoding="utf-8-sig") as k:
        w = csv.writer(f)
        kw = csv.writer(k)
        w.writerow(["id", "clip", "t", "text"])
        kw.writerow(["id", "arm", "source", "prompt_hash", "window", "image"])
        for n, it in enumerate(items, 1):
            w.writerow([n, it["clip"], it["t"], it["text"]])
            kw.writerow([n, it["arm"], it["source"], it["prompt_hash"], it["window"], it.get("image", "")])
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
    with open(root / "key.csv", newline="", encoding="utf-8-sig") as f:
        key = {int(r["id"]): r for r in csv.DictReader(f)}
    with open(root / "sheet.csv", newline="", encoding="utf-8-sig") as f:
        sheet = {int(r["id"]): r for r in csv.DictReader(f)}
    missing = sorted(set(key) - set(verdicts))
    if missing:
        raise SystemExit(f"{len(missing)} lines have no verdict, first: {missing[:10]}")
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, r in key.items():
        groups[r["arm"]].append(i)
    out_rows = []
    print(f"{'arm':<10} {'n':>3} {'yes':>4} {'no':>4} {'unc':>4} {'grounded':>9} {'95% Wilson':>12} {'notable':>8} {'mistake':>8}  failure types")
    for arm, ids in sorted(groups.items()):
        c = Counter(verdicts[i][0] for i in ids)
        n, yes = len(ids), c["yes"]
        lo, hi = wilson(yes, n)
        ft = Counter(verdicts[i][1] or "?" for i in ids if verdicts[i][0] != "yes")
        notable = sum(1 for i in ids if verdicts[i][3] == "yes")
        mistakes = sum(1 for i in ids if MISTAKE_WORDS.search(sheet[i]["text"]))
        print(f"{arm:<10} {n:>3} {yes:>4} {c['no']:>4} {c['unclear']:>4} {yes / n:>8.0%} {lo:>7.0%}-{hi:.0%} {notable / n:>8.0%} {mistakes:>8}  " + " ".join(f"{k}={v}" for k, v in sorted(ft.items())))
    for i in sorted(key):
        out_rows.append({**key[i], "clip": sheet[i]["clip"], "t": sheet[i]["t"], "text": sheet[i]["text"],
                         "verdict": verdicts[i][0], "ftype": verdicts[i][1], "reason": verdicts[i][2], "notable": verdicts[i][3],
                         "mistake_claim": "yes" if MISTAKE_WORDS.search(sheet[i]["text"]) else ""})
    with open(root / "unblinded.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"Wrote {root / 'unblinded.csv'}")


def humor(pool: str, groups: List[str], per_group: int, seed: int) -> None:
    """Owner humor sheet from a pool's lines: per_group random lines from each GROUP=arm,arm..., shuffled, origin hidden.
    Writes logs/grounding/owner_humor/<pool>/: sheet.csv (id, clip, t, text, strip, rating; rating empty), strips/NN.jpg,
    index.html (line and strip together) and key.csv (id -> group, arm, source)."""
    import html
    import shutil

    root = BLIND_DIR / pool
    with open(root / "key.csv", newline="", encoding="utf-8-sig") as f:
        key = {int(r["id"]): r for r in csv.DictReader(f)}
    with open(root / "sheet.csv", newline="", encoding="utf-8-sig") as f:
        sheet = {int(r["id"]): r for r in csv.DictReader(f)}
    rng = random.Random(seed)
    picked: List[Tuple[int, str]] = []
    for spec in groups:
        name, _, arms = spec.partition("=")
        ids = sorted(i for i, r in key.items() if r["arm"] in arms.split(","))
        picked += [(i, name) for i in rng.sample(ids, min(per_group, len(ids)))]
    rng.shuffle(picked)
    out = HUMOR_DIR / pool
    (out / "strips").mkdir(parents=True, exist_ok=True)
    cards = []
    with open(out / "sheet.csv", "w", newline="", encoding="utf-8-sig") as f, open(out / "key.csv", "w", newline="", encoding="utf-8-sig") as k:
        w, kw = csv.writer(f), csv.writer(k)
        w.writerow(["id", "clip", "t", "text", "strip", "rating (1-5)"])
        kw.writerow(["id", "group", "arm", "source", "pool_id"])
        for n, (i, group) in enumerate(picked, 1):
            src = key[i].get("image", "")
            strip = f"strips/{n:02d}.jpg"
            if src and (REPO_DIR / "logs" / "grounding" / src).exists():
                shutil.copy(REPO_DIR / "logs" / "grounding" / src, out / strip)
            else:
                strip = ""
            r = sheet[i]
            w.writerow([n, r["clip"], r["t"], r["text"], strip, ""])
            kw.writerow([n, group, key[i]["arm"], key[i]["source"], i])
            cards.append(f'<div><p><b>{n}</b> &nbsp; {html.escape(r["clip"])} t={float(r["t"]):.1f}s</p><p style="font-size:1.3em">{html.escape(r["text"])}</p>'
                         + (f'<img src="{strip}" style="max-width:100%">' if strip else "") + "<p>rating 1-5: ____</p><hr></div>")
    (out / "index.html").write_text('<meta charset="utf-8"><title>Humor sheet</title><body style="font-family:sans-serif;max-width:1100px;margin:auto">'
                                    "<h2>Rate each line 1 (not funny) to 5 (very funny)</h2>" + "".join(cards) + "</body>", encoding="utf-8")
    print(f"{len(picked)} lines in {out} (sheet.csv has an empty rating column; key.csv hides the origin)")


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
    h = sub.add_parser("humor")
    h.add_argument("pool")
    h.add_argument("--group", action="append", required=True, metavar="NAME=ARM,ARM", help="e.g. baseline=base_a,base_b")
    h.add_argument("--per-group", type=int, default=15)
    h.add_argument("--seed", type=int, default=38)
    a = p.parse_args()
    if a.cmd == "make":
        make(a.pool, a.clip_dir, a.arm, {k: int(v) for k, v in (s.split("=") for s in a.sample)}, a.seed)
    elif a.cmd == "humor":
        humor(a.pool, a.group, a.per_group, a.seed)
    else:
        unblind(a.pool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
