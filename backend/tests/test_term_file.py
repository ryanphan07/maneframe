import importlib.util
import json
import random
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import term_file as tf  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TEXT = """# comment
[slang]
cooked
aura
NPC
lowkey
chat
it's giving

[references]
The Lion King | simba
a Twitch stream | twitch
Mulan
Tekken
"""


def test_parse_sections_aliases_and_keys():
    t = tf.parse_terms(TEXT)
    assert [d for d, _ in t["slang"]] == ["cooked", "aura", "NPC", "lowkey", "chat", "it's giving"]
    assert t["references"][0] == ("The Lion King", ("lion king", "simba"))
    assert t["references"][1][1] == ("twitch stream", "twitch")


def test_parse_errors():
    for bad in ("cooked\n", "[slang]\ncooked\n", "[foo]\nx\n"):
        try:
            tf.parse_terms(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_shipped_term_file_parses_and_has_required_terms():
    t = tf.parse_terms((SCRIPTS / "eval_arms" / "s_terms.txt").read_text(encoding="utf-8"))
    refs = " ".join(d for d, _ in t["references"])
    for name in ("Lion King", "Kung Fu Panda", "Shang-Chi", "Mulan", "Tekken", "Street Fighter", "WWE", "Olympic gymnastics", "Twitch chat", "NBA 2K"):
        assert name in refs
    assert len(t["slang"]) >= 30 and len(t["references"]) >= 24


def test_pick_counts_and_exclusion():
    t = tf.parse_terms(TEXT)
    rng = random.Random(1)
    for _ in range(50):
        p = tf.pick_terms(rng, t, ["Pure simba energy.", "Yellow lion is cooked, chat."])
        assert len(p["slang"]) == 3 and len(p["references"]) == 2
        assert not {"cooked", "chat"} & set(p["slang"])
        assert "The Lion King" not in p["references"]  # alias simba matched


def test_pick_tops_up_when_pool_exhausted():
    t = tf.parse_terms(TEXT)
    p = tf.pick_terms(random.Random(0), t, ["cooked aura NPC lowkey chat it's giving Mulan Tekken simba twitch"])
    assert len(p["slang"]) == 3 and len(p["references"]) == 2


def test_seeded_picks_repeat():
    t = tf.parse_terms(TEXT)
    a = [tf.pick_terms(random.Random(5), t, []) for _ in range(3)]
    assert a == [tf.pick_terms(random.Random(5), t, []) for _ in range(3)]
    assert tf.pick_terms(random.Random(5), t, []) != tf.pick_terms(random.Random(6), t, [])


def test_options_text_has_options_and_instruction():
    txt = tf.options_text({"slang": ["cooked", "aura", "NPC"], "references": ["Mulan", "Tekken"]})
    assert txt.startswith("Options for this window (use at most one slang term and one reference per line; use at least one reference this window if it fits what you see): ")
    assert "cooked, aura, NPC" in txt and "Mulan, Tekken" in txt


def test_rotation_window_history_and_intro_text():
    ev = _load("eval_commentary")
    from app.services import llm

    t = tf.parse_terms(TEXT)
    rot = ev.Rotation(t, seed=3)
    rot.finish_window(["Blue lion is cooked."])
    rot.finish_window(["Pure aura."])
    rot.finish_window(["Mulan vibes. NPC mode."])
    opts = rot.next_window("a.mp4", 4)  # last 3 windows used cooked, aura, Mulan, NPC
    slang, refs = opts.split("slang: ")[1].split(". references: ")
    assert not {"cooked", "aura", "NPC"} & set(slang.split(", "))
    assert "Mulan" not in refs
    orig = llm.build_messages
    try:
        ev.install_rotation(rot)
        msgs = llm.build_messages([], 0.0, 5.0, [])
        assert opts in msgs[1]["content"][0]["text"]
    finally:
        llm.build_messages = orig
        ev.ROTATION = None
    assert rot.log[-1]["window"] == 4 and json.dumps(rot.log)


def test_terms_command_counts_with_term_file(tmp_path):
    br = _load("blind_review")
    slang, refs = br.term_lists(SCRIPTS / "eval_arms" / "s_terms.txt")
    assert "rizz" in slang and "Mulan" in refs
    texts = ["Blue lion is cooked. Mulan vibes.", "Pink lion, tutorial boss. Mulan again.", "Yellow lion rests."]
    c = br.count_terms(texts, slang, refs)
    assert (c["slang_lines"], c["reference_lines"], c["distinct_references"], c["repeated_references"]) == (2, 2, 1, 1)


def test_slang_inside_reference_phrase_is_not_slang(tmp_path):
    br = _load("blind_review")
    slang, refs = br.term_lists(SCRIPTS / "eval_arms" / "s_terms.txt")
    c = br.count_terms(["The crowd roars like Twitch chat.", "A Twitch stream of drums.", "Chat is cooked."], slang, refs)
    assert c["slang"] == {"chat": 1, "cooked": 1} and c["slang_lines"] == 1 and c["reference_lines"] == 2
