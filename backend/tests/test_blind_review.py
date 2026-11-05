import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import blind_review as br  # noqa: E402
import eval_commentary as ev  # noqa: E402


def test_wilson_known_values():
    lo, hi = br.wilson(59, 119)
    assert round(lo, 2) == 0.41 and round(hi, 2) == 0.58  # the session 36 baseline interval
    assert br.wilson(0, 0) == (0.0, 0.0)
    lo, hi = br.wilson(10, 10)
    assert hi == pytest.approx(1.0) and lo < 0.8


def test_parse_verdicts_types_notable_and_reasons():
    got = br.parse_verdicts("# note\n1 yes nt\n2 no pose wrong lion reared\n3 UNCLEAR face bo ears\n4 no because it moved\n5 no named nt 7-star not shown\n6 yes bo nt\n")
    assert got[1] == ("yes", "", "", "yes")
    assert got[2] == ("no", "pose", "wrong lion reared", "")
    assert got[3] == ("unclear", "face", "ears", "no")
    assert got[4] == ("no", "", "because it moved", "")
    assert got[5] == ("no", "named", "7-star not shown", "yes")
    assert got[6] == ("yes", "", "nt", "no")  # a second notable token is text, not a second answer
    with pytest.raises(ValueError):
        br.parse_verdicts("5 maybe")


def test_mistake_words_match_claims_of_a_mistake():
    for text in ("Yikes, someone had an oopsie", "That lion is lost", "Yellow is behind the beat", "forgot the choreography"):
        assert br.MISTAKE_WORDS.search(text), text
    assert not br.MISTAKE_WORDS.search("Blue rears up beside the drummers")


def test_format_facts_pairs_entries_with_frame_times():
    data = {"frames": [{"lions": ["blue left crouched", "pink right reared_up"], "drummers": True}, {"lions": [], "drummers": False}]}
    text = ev.format_facts(data, [0.8, 2.3])
    assert "t=0.8s: blue left crouched; pink right reared up; drummers visible" in text
    assert "t=2.3s: no lions on stage; no drummers visible" in text


def test_facts_schema_lion_enum_is_closed():
    items = ev.FACTS_SCHEMA["json_schema"]["schema"]["properties"]["frames"]["items"]["properties"]["lions"]["items"]
    assert "blue left crouched" in items["enum"] and len(items["enum"]) == 60


def test_build_messages_adds_facts_only_when_given():
    from app.services import llm

    frames = [{"timestamp": 0.5, "image_data": "AA=="}]
    plain = llm.build_messages(frames, 0, 10, [])[1]["content"][0]["text"]
    withf = llm.build_messages(frames, 0, 10, [], facts="FACTS HERE")[1]["content"][0]["text"]
    assert "FACTS HERE" not in plain and "FACTS HERE" in withf


def test_csv_readers_accept_files_with_and_without_a_bom(tmp_path):
    for name, enc in (("plain.csv", "utf-8"), ("bom.csv", "utf-8-sig")):
        (tmp_path / name).write_text("clip,text\na.mp4,Blue’s turn\n", encoding=enc, newline="")
    rows = ev.read_rows([tmp_path / "plain.csv", tmp_path / "bom.csv"])
    assert [r["text"] for r in rows] == ["Blue’s turn"] * 2
    assert all(list(r) == ["clip", "text"] for r in rows)  # no "﻿clip" key


def test_every_script_csv_file_is_written_and_read_as_utf8_sig():
    """Excel shows curly apostrophes as mojibake unless the file starts with a BOM; csv files are opened with newline=''."""
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    for path in scripts.glob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if 'newline=""' in line:
                assert 'encoding="utf-8-sig"' in line, f"{path.name}:{n} opens a csv without utf-8-sig"


def test_humor_sheet_hides_origin_and_has_empty_rating_column(tmp_path, monkeypatch):
    import csv

    monkeypatch.setattr(br, "BLIND_DIR", tmp_path / "blind")
    monkeypatch.setattr(br, "HUMOR_DIR", tmp_path / "humor")
    pool = tmp_path / "blind" / "p"
    pool.mkdir(parents=True)
    with open(pool / "key.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "arm", "source", "prompt_hash", "window", "image"])
        for i in range(1, 9):
            w.writerow([i, "old" if i <= 4 else "new", "x.csv", "h", 1, ""])
    with open(pool / "sheet.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "clip", "t", "text"])
        for i in range(1, 9):
            w.writerow([i, "a.mp4", "1.0", f"Blue’s line {i}"])
    br.humor("p", ["baseline=old", "new=new"], 3, 1)
    out = tmp_path / "humor" / "p"
    rows = list(csv.DictReader(open(out / "sheet.csv", encoding="utf-8-sig")))
    key = list(csv.DictReader(open(out / "key.csv", encoding="utf-8-sig")))
    assert len(rows) == 6 and all(r["rating (1-5)"] == "" for r in rows)
    assert sorted(k["group"] for k in key) == ["baseline"] * 3 + ["new"] * 3
    assert "arm" not in rows[0] and "group" not in rows[0]
