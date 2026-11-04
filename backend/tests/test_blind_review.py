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


def test_parse_verdicts_types_and_reasons():
    got = br.parse_verdicts("# note\n1 yes\n2 no pose wrong lion reared\n3 UNCLEAR face ears\n4 no because it moved\n")
    assert got[1] == ("yes", "", "")
    assert got[2] == ("no", "pose", "wrong lion reared")
    assert got[3] == ("unclear", "face", "ears")
    assert got[4] == ("no", "", "because it moved")
    with pytest.raises(ValueError):
        br.parse_verdicts("5 maybe")


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
