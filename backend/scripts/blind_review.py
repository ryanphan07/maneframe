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
import json
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


RATING_COL = "rating (1-5)"

HUMOR_PAGE = r"""<!doctype html><meta charset="utf-8"><title>Humor sheet</title>
<body style="font-family:sans-serif;max-width:1100px;margin:auto">
<h2>Rate each line 1 (not funny) to 5 (very funny)</h2>
<p>Ratings are kept in this browser as you go. When done, press <b>Save ratings</b>: choose <code>sheet.csv</code> in <code>@@FOLDER@@</code>
and overwrite it (browsers that cannot write files will download <code>sheet.csv</code>; move it into that folder, replacing the old one).</p>
<p><button id="save" style="font-size:1.2em">Save ratings</button> <span id="msg"></span> <span id="count"></span></p>
@@CARDS@@
<script>
const ROWS = @@ROWS@@, STORE = @@STORE@@;
let saved = {}; try { saved = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) {}
const sels = document.querySelectorAll("select[data-id]");
function count() { document.getElementById("count").textContent = [...sels].filter(s => s.value).length + " of " + sels.length + " rated"; }
sels.forEach(s => { if (saved[s.dataset.id]) s.value = saved[s.dataset.id];
  s.addEventListener("change", () => { saved[s.dataset.id] = s.value; try { localStorage.setItem(STORE, JSON.stringify(saved)); } catch (e) {} count(); }); });
count();
function csv() {
  const q = v => '"' + String(v).replace(/"/g, '""') + '"';
  const lines = [["id", "clip", "t", "text", "strip", "rating (1-5)"].join(",")];
  ROWS.forEach(r => lines.push([r.id, q(r.clip), r.t, q(r.text), r.strip, (saved[r.id] || "")].join(",")));
  return "\ufeff" + lines.join("\r\n") + "\r\n";
}
document.getElementById("save").onclick = async () => {
  const msg = document.getElementById("msg"), data = csv();
  try {
    const h = await window.showSaveFilePicker({suggestedName: "sheet.csv", types: [{description: "CSV", accept: {"text/csv": [".csv"]}}]});
    const w = await h.createWritable(); await w.write(data); await w.close(); msg.textContent = "Saved. Make sure it is @@FOLDER@@/sheet.csv"; return;
  } catch (e) { if (e && e.name === "AbortError") return; }
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([data], {type: "text/csv"})); a.download = "sheet.csv"; a.click();
  msg.textContent = "Downloaded sheet.csv: move it to @@FOLDER@@ (replace the old one).";
};
</script></body>"""


def read_anchors(sheet_path: Path, count: int, rng: random.Random) -> List[Dict[str, str]]:
    """`count` random rated lines from an earlier owner humor sheet (rating column filled), each with the path of its strip
    and the rating the owner gave. Their origin in that sheet's own key.csv is carried along when the key exists."""
    with open(sheet_path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get(RATING_COL) or "").strip()]
    origin: Dict[str, Dict[str, str]] = {}
    key_path = sheet_path.parent / "key.csv"
    if key_path.exists():
        with open(key_path, newline="", encoding="utf-8-sig") as f:
            origin = {r["id"]: r for r in csv.DictReader(f)}
    picked = rng.sample(rows, min(count, len(rows)))
    for r in picked:
        r["strip_path"] = str(sheet_path.parent / r["strip"]) if r.get("strip") else ""
        r["origin"] = (origin.get(r["id"]) or {}).get("arm", "")
    return picked


def humor(pool: str, groups: List[str], per_group: int, seed: int, anchors: "Path | None" = None, anchor_count: int = 10,
          from_pools: "List[str] | None" = None) -> None:
    """Owner humor sheet from a pool's lines: per_group random lines from each GROUP=arm,arm..., shuffled, origin hidden.
    With `anchors` (an earlier, already rated owner sheet), `anchor_count` of its rated lines are shuffled in; they are the
    reference for how this owner rates, and key.csv records them as group "anchor" with the old rating.
    Writes logs/grounding/owner_humor/<pool>/: sheet.csv (id, clip, t, text, strip, rating; rating empty), strips/NN.jpg,
    index.html (line and strip together) and key.csv (id -> group, arm, source, old rating)."""
    import html
    import shutil

    # `from_pools` (default: the pool itself) lets one owner sheet draw lines from several blind pools; arm names must differ
    # between them. Ids are renumbered to (pool index * 10000 + id) so they stay unique; pool_id in key.csv keeps "pool:id".
    key: Dict[int, Dict[str, str]] = {}
    sheet: Dict[int, Dict[str, str]] = {}
    for n, src_pool in enumerate(from_pools or [pool]):
        root = BLIND_DIR / src_pool
        with open(root / "key.csv", newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                key[n * 10000 + int(r["id"])] = {**r, "pool": src_pool}
        with open(root / "sheet.csv", newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                sheet[n * 10000 + int(r["id"])] = r
    rng = random.Random(seed)
    picked: List[Dict[str, str]] = []
    for spec in groups:
        name, _, arms = spec.partition("=")
        wanted = arms.split(",")  # "arm" or "pool:arm" (needed when two pools reuse an arm name)
        ids = sorted(i for i, r in key.items() if r["arm"] in wanted or f"{r['pool']}:{r['arm']}" in wanted)
        for i in rng.sample(ids, min(per_group, len(ids))):
            src = key[i].get("image", "")
            full = REPO_DIR / "logs" / "grounding" / src
            picked.append({"group": name, "clip": sheet[i]["clip"], "t": sheet[i]["t"], "text": sheet[i]["text"],
                           "strip_path": str(full) if src and full.exists() else "", "arm": key[i]["arm"],
                           "source": key[i]["source"], "pool_id": f"{key[i]['pool']}:{i % 10000}", "old_rating": ""})
    if anchors is not None:
        for r in read_anchors(anchors, anchor_count, rng):
            picked.append({"group": "anchor", "clip": r["clip"], "t": r["t"], "text": r["text"], "strip_path": r["strip_path"],
                           "arm": r["origin"], "source": str(anchors), "pool_id": r["id"], "old_rating": r[RATING_COL]})
    rng.shuffle(picked)
    out = HUMOR_DIR / pool
    (out / "strips").mkdir(parents=True, exist_ok=True)
    cards = []
    rows_out: List[Dict[str, object]] = []
    with open(out / "sheet.csv", "w", newline="", encoding="utf-8-sig") as f, open(out / "key.csv", "w", newline="", encoding="utf-8-sig") as k:
        w, kw = csv.writer(f), csv.writer(k)
        w.writerow(["id", "clip", "t", "text", "strip", RATING_COL])
        kw.writerow(["id", "group", "arm", "source", "pool_id", "old_rating"])
        for n, r in enumerate(picked, 1):
            strip = f"strips/{n:02d}.jpg"
            if r["strip_path"] and Path(r["strip_path"]).exists():
                shutil.copy(r["strip_path"], out / strip)
            else:
                strip = ""
            w.writerow([n, r["clip"], r["t"], r["text"], strip, ""])
            kw.writerow([n, r["group"], r["arm"], r["source"], r["pool_id"], r["old_rating"]])
            rows_out.append({"id": n, "clip": r["clip"], "t": r["t"], "text": r["text"], "strip": strip})
            cards.append(f'<div class="card"><p><b>{n}</b> &nbsp; {html.escape(r["clip"])} t={float(r["t"]):.1f}s</p><p style="font-size:1.3em">{html.escape(r["text"])}</p>'
                         + (f'<img src="{strip}" style="max-width:100%">' if strip else "")
                         + f'<p>rating: <select data-id="{n}"><option value="">-</option>'
                         + "".join(f"<option>{v}</option>" for v in range(1, 6)) + "</select></p><hr></div>")
    (out / "index.html").write_text(HUMOR_PAGE.replace("@@CARDS@@", "".join(cards)).replace("@@ROWS@@", json.dumps(rows_out)).replace("@@FOLDER@@", html.escape(str(out)))
                                    .replace("@@STORE@@", json.dumps(f"humor-{pool}")), encoding="utf-8")
    print(f"{len(picked)} lines in {out} (sheet.csv has an empty rating column; key.csv hides the origin). Rate in index.html, then Save ratings and replace {out / 'sheet.csv'}")


SLANG = ["cooked", "aura farming", "aura", "locked in", "NPC", "main character", "side quest", "crash out", "it's giving", "ate",
         "no cap", "lowkey", "unc", "chat", "buffering", "Fanum tax", "Fanum"]
REFERENCES = {"Lion King": "lion king|simba|mufasa|hakuna", "Kung Fu Panda": "kung fu panda|master shifu|dragon warrior",
              "Mario Kart": "mario ?kart|mario|blue shell|rainbow road", "Minecraft": "minecraft|creeper", "NBA": "nba|dunk|lebron|curry|free throw|buzzer",
              "Squid Game": "squid game|red light|green light", "KPop Demon Hunters": "k-?pop|demon hunters|huntrix",
              "Labubu": "labubu", "Twitch": "twitch|streamer|stream|donation|subscribers?"}


def term_lists(path: Path) -> Tuple[List[str], Dict[str, str]]:
    """SLANG-style list and REFERENCES-style dict from a term file (eval_arms/s_terms.txt), merged with the built-in arm s lists."""
    import term_file

    parsed = term_file.parse_terms(path.read_text(encoding="utf-8"))
    slang = list(SLANG)
    for display, _keys in parsed["slang"]:
        if not any(display.lower() == t.lower() for t in slang):
            slang.append(display)
    refs = dict(REFERENCES)
    known = {_norm(n) for n in REFERENCES}
    for display, keys in parsed["references"]:
        if _norm(display) in known or any(_norm(display) in pat.split("|") for pat in REFERENCES.values()):
            continue
        refs[display] = "|".join(re.escape(k) for k in keys)
    return slang, refs


def _norm(display: str) -> str:
    return re.sub(r"^(?:a|an|the)\s+", "", display.lower())


def count_terms(texts: List[str], slang_terms=None, references=None) -> Dict[str, object]:
    """Lines with a slang term, lines with a reference, and references repeated within the run (mentions minus distinct references).
    Matching is by word, case-insensitive; 'aura farming' also counts as 'aura' only once per line, and a slang word inside a reference match ("Twitch chat") is not slang."""
    slang_terms = slang_terms or SLANG
    references = references or REFERENCES
    slang_re = [re.compile(r"(?<![\w'])" + re.escape(t) + r"(?![\w'])", re.I) for t in slang_terms]
    ref_re = {name: re.compile(r"(?<!\w)(?:" + pat + r")(?!\w)", re.I) for name, pat in references.items()}
    slang_lines = ref_lines = 0
    mentions: Counter = Counter()
    slang_mentions: Counter = Counter()
    for text in texts:
        ref_spans = [m.span() for rx in ref_re.values() for m in rx.finditer(text)]
        # a slang word inside a reference phrase ("Twitch chat") belongs to the reference, not the slang count
        hit = [t for t, rx in zip(slang_terms, slang_re)
               if any(not any(a <= m.start() and m.end() <= b for a, b in ref_spans) for m in rx.finditer(text))]
        hit = [t for t in hit if not (t in ("aura", "Fanum") and any(o != t and t in o and o in hit for o in slang_terms))]
        slang_lines += bool(hit)
        slang_mentions.update(hit)
        refs = [name for name, rx in ref_re.items() if rx.search(text)]
        ref_lines += bool(refs)
        mentions.update(refs)
    return {"lines": len(texts), "slang_lines": slang_lines, "reference_lines": ref_lines,
            "repeated_references": sum(c - 1 for c in mentions.values()), "repeated_slang": sum(c - 1 for c in slang_mentions.values()),
            "distinct_slang": len(slang_mentions), "distinct_references": len(mentions),
            "references": dict(mentions), "slang": dict(slang_mentions)}


def terms(runs: List[str], terms_file: Path = None) -> None:
    """Per run (LABEL=eval csv): lines, lines with slang / a reference, distinct terms, repeats within the run (a term in
    more than one line). With --terms-file every run is matched against the same merged lists, so arms compare fairly."""
    slang, refs = term_lists(terms_file) if terms_file else (None, None)
    print(f"{'run':<12} {'lines':>5} {'slang':>6} {'refs':>5} {'dist slang':>10} {'dist refs':>9} {'slang rep':>9} {'ref rep':>8}")
    for spec in runs:
        label, _, path = spec.partition("=")
        with open(path, newline="", encoding="utf-8-sig") as f:
            texts = [r["text"] for r in csv.DictReader(f)]
        c = count_terms(texts, slang, refs)
        print(f"{label:<12} {c['lines']:>5} {c['slang_lines']:>6} {c['reference_lines']:>5} {c['distinct_slang']:>10} {c['distinct_references']:>9} "
              f"{c['repeated_slang']:>9} {c['repeated_references']:>8}")
        print(f"    references: {c['references']}")


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
    h.add_argument("--anchors", type=Path, help="an earlier rated owner sheet.csv; --anchor-count of its rated lines are shuffled in as the reference")
    h.add_argument("--from-pool", action="append", help="draw lines from this blind pool (repeatable; default: the pool named above)")
    h.add_argument("--anchor-count", type=int, default=10)
    t = sub.add_parser("terms", help="count slang terms and pop-culture references per run")
    t.add_argument("run", nargs="+", metavar="LABEL=CSV")
    t.add_argument("--terms-file", type=Path, help="also match every term in this file (eval_arms/s_terms.txt)")
    a = p.parse_args()
    if a.cmd == "make":
        make(a.pool, a.clip_dir, a.arm, {k: int(v) for k, v in (s.split("=") for s in a.sample)}, a.seed)
    elif a.cmd == "humor":
        humor(a.pool, a.group, a.per_group, a.seed, a.anchors, a.anchor_count, a.from_pool)
    elif a.cmd == "terms":
        terms(a.run, a.terms_file)
    else:
        unblind(a.pool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
