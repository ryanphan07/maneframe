import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("grounding_check", Path(__file__).resolve().parent.parent / "scripts" / "grounding_check.py")
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)


def test_strip_times_clamped_into_clip():
    assert gc.strip_times(0.5, 40.0) == [0.0, 0.5, 2.0]
    assert gc.strip_times(39.9, 40.0) == [38.4, 39.9 if 39.9 <= 39.95 else 39.95, 39.95]


def test_tally_rate_per_hash_and_style_counts_unclear_as_not_grounded():
    rows = [
        {"prompt_hash": "a", "style": "hype", "grounded": "yes"},
        {"prompt_hash": "a", "style": "hype", "grounded": " No "},
        {"prompt_hash": "a", "style": "hype", "grounded": "unclear"},
        {"prompt_hash": "a", "style": "hype", "grounded": ""},
        {"prompt_hash": "b", "style": "hype", "grounded": "yes"},
    ]
    groups = gc.tally(rows)
    assert [(g["prompt_hash"], g["n"], g["yes"], g["no"], g["unclear"]) for g in groups] == [("a", 3, 1, 1, 1), ("b", 1, 1, 0, 0)]
    assert abs(groups[0]["rate"] - 1 / 3) < 1e-9
