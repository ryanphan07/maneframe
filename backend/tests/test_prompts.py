import json

import pytest

from app.config import settings
from app.services import commentary as commentary_module
from app.services import llm as llm_module

FRAMES = [{"timestamp": 0.5, "image_data": "QUJD"}]


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    """A prompts folder the test can edit: base prompt, glossary, examples and two styles."""
    (tmp_path / "base.txt").write_text("BASE MAX_QUIPS", encoding="utf-8")
    (tmp_path / "glossary.txt").write_text("DRAFT: owner to review\n- cai qing: plucking the greens\n", encoding="utf-8")
    (tmp_path / "examples.txt").write_text("# format comment\n", encoding="utf-8")
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "hype.txt").write_text("Loud.\n", encoding="utf-8")
    (styles / "deadpan.txt").write_text("Flat.\n", encoding="utf-8")
    monkeypatch.setattr(llm_module, "PROMPT_PATH", tmp_path / "base.txt")
    monkeypatch.setattr(llm_module, "GLOSSARY_PATH", tmp_path / "glossary.txt")
    monkeypatch.setattr(llm_module, "EXAMPLES_PATH", tmp_path / "examples.txt")
    monkeypatch.setattr(llm_module, "STYLES_DIR", styles)
    llm_module.clear_prompt_cache()
    yield tmp_path
    llm_module.clear_prompt_cache()


def test_prompt_has_glossary_without_draft_note_and_style_but_no_empty_examples(prompt_dir):
    prompt = llm_module.load_system_prompt("deadpan")
    assert f"BASE {settings.MAX_QUIPS_PER_WINDOW}" in prompt
    assert "- cai qing: plucking the greens" in prompt
    assert "DRAFT" not in prompt
    assert "EXAMPLES" not in prompt
    assert prompt.endswith("COMMENTARY STYLE FOR THIS RUN\nFlat.")


def test_examples_are_appended_once_they_have_real_lines(prompt_dir):
    (prompt_dir / "examples.txt").write_text('# comment\n{"t": 1.0, "text": "A real one"}\n', encoding="utf-8")
    llm_module.clear_prompt_cache()
    prompt = llm_module.load_system_prompt()
    assert '{"t": 1.0, "text": "A real one"}' in prompt
    assert "# comment" not in prompt
    assert prompt.index("MORE EXAMPLES") < prompt.index("COMMENTARY STYLE")


def test_unknown_style_is_rejected_and_styles_listed(prompt_dir):
    assert llm_module.list_styles() == ["deadpan", "hype"]
    with pytest.raises(ValueError):
        llm_module.load_system_prompt("../base")


def test_prompt_hash_changes_with_style_and_content(prompt_dir):
    hype, deadpan = llm_module.prompt_hash("hype"), llm_module.prompt_hash("deadpan")
    assert hype != deadpan and len(hype) == 8
    (prompt_dir / "glossary.txt").write_text("- jong: poles\n", encoding="utf-8")
    llm_module.clear_prompt_cache()
    assert llm_module.prompt_hash("hype") != hype


def test_real_prompt_files_load_and_every_style_builds():
    llm_module.clear_prompt_cache()
    assert llm_module.list_styles() == ["broadcast", "deadpan", "hype"]
    for style in llm_module.list_styles():
        prompt = llm_module.load_system_prompt(style)
        assert "cai qing" in prompt.lower() and "DRAFT" not in prompt
        assert "OUTPUT: only JSON" in prompt


def _intro(messages):
    return messages[1]["content"][0]["text"]


def test_messages_label_earlier_context_and_forbid_repeats(prompt_dir):
    messages = llm_module.build_messages(
        FRAMES, 20.0, 30.0, ["quip a", "quip b"], earlier_seen=["lion one bows", "lion two rears"], style="deadpan",
    )
    intro = _intro(messages)
    assert "Earlier context" in intro and json.dumps(["lion one bows", "lion two rears"]) in intro
    assert "do not repeat" in intro and json.dumps(["quip a", "quip b"]) in intro
    assert intro.index("Earlier context") < intro.index("quip a")
    assert messages[0]["content"].endswith("Flat.")


def test_messages_without_context_have_neither_section(prompt_dir):
    intro = _intro(llm_module.build_messages(FRAMES, 0.0, 10.0, []))
    assert "Earlier context" not in intro and "previous quips" not in intro


def test_continuity_keeps_last_two_seen_and_quips_and_skips_empty_windows():
    ctx = commentary_module.Continuity()
    result = llm_module.WindowResult
    ctx.update(result([{"timestamp": 1, "text": "a"}], "seen 1"))
    ctx.update(result([{"timestamp": 2, "text": "b"}, {"timestamp": 3, "text": "c"}, {"timestamp": 4, "text": "d"}], "seen 2"))
    ctx.update(result([], "seen 3"))  # a quiet window still counts as seen; it does not erase the last quips
    assert ctx.seen == ["seen 2", "seen 3"]
    assert ctx.quips == ["c", "d"]
    ctx.update(result([{"timestamp": 5, "text": "e"}], None))  # no summary: seen is unchanged
    assert ctx.seen == ["seen 2", "seen 3"]
    assert ctx.quips == ["e"]


@pytest.mark.anyio
async def test_pipeline_passes_earlier_seen_and_style_to_each_window(monkeypatch):
    vp = commentary_module.video_processor_service
    monkeypatch.setattr(vp, "get_video_duration", lambda p: 30.0)
    monkeypatch.setattr(vp, "compute_crop_box", lambda p, d: None)
    monkeypatch.setattr(vp, "sample_window_frames", lambda p, s, e, n, w, box=None: [{"timestamp": s, "image_data": "abc"}])

    async def tts(quip, video_id=None):
        return {**quip, "audio_url": "http://x/a.mp3"}

    monkeypatch.setattr(commentary_module.tts_service, "generate_audio_clip", tts)
    calls = []

    async def fake(frames, start, end, previous=None, label="", earlier_seen=None, style=None):
        calls.append((list(previous), list(earlier_seen), style))
        i = len(calls)
        return llm_module.WindowResult([{"timestamp": start + 1, "text": f"quip {i}"}], f"seen {i}")

    monkeypatch.setattr(llm_module, "analyze_window", fake)
    result = await commentary_module.process_video_commentary("v.mp4", style="deadpan")

    assert calls == [
        ([], [], "deadpan"),
        (["quip 1"], ["seen 1"], "deadpan"),
        (["quip 2"], ["seen 1", "seen 2"], "deadpan"),
    ]
    assert result.style == "deadpan"


@pytest.mark.anyio
async def test_analyze_window_returns_seen_summary(monkeypatch, prompt_dir):
    reply = '{"seen": "  blue lion\\n bows ", "quips": [{"t": 1.0, "text": "Bow."}]}'

    class Completions:
        async def create(self, **kwargs):
            message = type("M", (), {"content": reply})()
            return type("R", (), {"choices": [type("C", (), {"message": message})()], "usage": None})()

    client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    monkeypatch.setattr(llm_module, "llm_client", client)
    result = await llm_module.analyze_window(FRAMES, 0.0, 3.0)
    assert result.seen == "blue lion bows"
    assert [q["text"] for q in result.quips] == ["Bow."]


def test_styles_route_lists_the_prompt_files():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/styles")
    assert resp.status_code == 200
    assert resp.json() == {"styles": llm_module.list_styles(), "default": llm_module.DEFAULT_STYLE, "max_upload_mb": settings.MAX_UPLOAD_MB}
    assert {"hype", "deadpan", "broadcast"} <= set(resp.json()["styles"])
