import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import blind_review as br  # noqa: E402


def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_humor_mixes_rated_anchors_and_keeps_their_origin_in_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(br, "BLIND_DIR", tmp_path / "blind")
    monkeypatch.setattr(br, "HUMOR_DIR", tmp_path / "humor")
    pool = tmp_path / "blind" / "p"
    write_csv(pool / "key.csv", ["id", "arm", "source", "prompt_hash", "window", "image"], [[i, "s", "x.csv", "h", 1, ""] for i in range(1, 6)])
    write_csv(pool / "sheet.csv", ["id", "clip", "t", "text"], [[i, "a.mp4", "1.0", f"new line {i}"] for i in range(1, 6)])
    old = tmp_path / "old"
    write_csv(old / "sheet.csv", ["id", "clip", "t", "text", "strip", "rating (1-5)"],
              [[1, "b.mp4", "2.0", "old a", "", "3"], [2, "b.mp4", "3.0", "old b", "", "2"], [3, "b.mp4", "4.0", "old unrated", "", ""]])
    write_csv(old / "key.csv", ["id", "group", "arm", "source", "pool_id"], [[1, "new", "n2", "z", 9], [2, "baseline", "base_a", "z", 8], [3, "new", "n2", "z", 7]])
    br.humor("p", ["s=s"], 3, 1, anchors=old / "sheet.csv", anchor_count=5)
    out = tmp_path / "humor" / "p"
    sheet = list(csv.DictReader(open(out / "sheet.csv", encoding="utf-8-sig")))
    key = list(csv.DictReader(open(out / "key.csv", encoding="utf-8-sig")))
    assert len(sheet) == 5  # 3 arm lines + the 2 rated anchors; the unrated line is not an anchor
    assert all(r["rating (1-5)"] == "" for r in sheet)  # old ratings are not shown to the rater
    anchors = [k for k in key if k["group"] == "anchor"]
    assert sorted((k["old_rating"], k["arm"]) for k in anchors) == [("2", "base_a"), ("3", "n2")]
    texts = {r["id"]: r["text"] for r in sheet}
    assert {texts[k["id"]] for k in anchors} == {"old a", "old b"}


def test_count_terms_slang_references_and_repeats():
    c = br.count_terms([
        "Three lions crouched low. Chat, are they buffering?",
        "Blue lion reared up. Pure aura farming.",
        "Yellow lion on the red envelope. Fanum tax collected.",
        "Pink lion dunks it like the NBA.",
        "Another NBA moment, the Mario Kart finish.",
        "Empty stage. The lions logged off.",
    ])
    assert c["lines"] == 6 and c["slang_lines"] == 3
    assert c["reference_lines"] == 2 and c["references"] == {"NBA": 2, "Mario Kart": 1}
    assert c["repeated_references"] == 1
    assert "aura" not in c["slang"] and c["slang"]["aura farming"] == 1  # a longer term hides its part
    assert br.count_terms(["The stage is a stream of chat-free lions"])["reference_lines"] == 1  # word match: stream
    assert br.count_terms(["Created, dated, unclear"])["slang_lines"] == 0  # no substring hits (ate, unc)
