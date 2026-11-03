import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("eval_commentary", Path(__file__).resolve().parent.parent / "scripts" / "eval_commentary.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def row(h, style, rating):
    return {"clip": "a.mp4", "window": "1", "t": "1.0", "style": style, "prompt_hash": h, "text": "x", "rating": rating}


def test_summarize_means_per_prompt_hash_and_style_ignoring_unrated_rows():
    rows = [
        row("aaaa1111", "hype", "4"), row("aaaa1111", "hype", "2"), row("aaaa1111", "hype", ""),
        row("bbbb2222", "hype", "5"),
        row("cccc3333", "deadpan", " 3.5 "), row("cccc3333", "deadpan", "n/a"),
    ]
    groups = ev.summarize(rows)
    assert groups == [
        {"prompt_hash": "cccc3333", "style": "deadpan", "n": 1, "mean": 3.5},
        {"prompt_hash": "bbbb2222", "style": "hype", "n": 1, "mean": 5.0},
        {"prompt_hash": "aaaa1111", "style": "hype", "n": 2, "mean": 3.0},
    ]


def test_summarize_with_nothing_rated_says_so():
    assert ev.summarize([row("aaaa1111", "hype", "")]) == []
    assert "No rated rows" in ev.format_summary([])


def test_format_summary_lists_every_group():
    text = ev.format_summary(ev.summarize([row("aaaa1111", "hype", "4"), row("aaaa1111", "hype", "5")]))
    assert "aaaa1111" in text and "hype" in text and "4.50" in text


def test_parse_clip_list_skips_blanks_and_comments():
    text = "# my clips\n\nC:\\clips\\a.mp4\n  C:\\clips\\b.mp4  \n   # off\n"
    assert ev.parse_clip_list(text) == ["C:\\clips\\a.mp4", "C:\\clips\\b.mp4"]


def test_summarize_reads_rated_csv_files(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(
        "clip,window,t,style,prompt_hash,text,rating\n"
        'a.mp4,1,1.0,hype,aaaa1111,"Gold lion, bows",4\n'
        "a.mp4,2,12.0,hype,aaaa1111,Another,\n",
        encoding="utf-8",
    )
    assert ev.summarize(ev.read_rows([path])) == [{"prompt_hash": "aaaa1111", "style": "hype", "n": 1, "mean": 4.0}]
