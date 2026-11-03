import pytest
from app.schemas.events import CommentaryResponse
from app.services import commentary as commentary_module
from app.services import llm as llm_module
from app.services import tts as tts_module


async def fake_tts(quip, video_id=None):
    return {"timestamp": quip["timestamp"], "text": quip["text"], "audio_url": "http://example.com/clip.mp3"}


@pytest.fixture
def fake_video(monkeypatch):
    """20s video, two 10s windows, one fake frame per window; the LLM is replaced per test."""
    vp = commentary_module.video_processor_service
    monkeypatch.setattr(vp, "get_video_duration", lambda p: 20.0)
    monkeypatch.setattr(vp, "compute_crop_box", lambda p, d: None)
    monkeypatch.setattr(vp, "sample_window_frames", lambda p, s, e, n, w, box=None: [{"timestamp": s, "image_data": "abc"}])


def fake_llm_returning(by_window):
    async def fake(frames, start, end, previous=None, label="", **_):
        return llm_module.WindowResult(list(by_window[int(start // 10)]))
    return fake


TWO_WINDOWS = [
    [{"timestamp": 1.0, "text": "first quip"}],
    [{"timestamp": 12.0, "text": "second quip"}],
]


@pytest.mark.anyio
async def test_process_video_commentary_maps_audio_to_quips(monkeypatch, fake_video):
    async def fake_generate_audio_clip(quip, video_id="default"):
        return {"timestamp": quip["timestamp"], "text": quip["text"], "audio_url": f"http://example.com/{quip['timestamp']}.mp3"}

    monkeypatch.setattr(llm_module, "analyze_window", fake_llm_returning(TWO_WINDOWS))
    monkeypatch.setattr(tts_module, "generate_audio_clip", fake_generate_audio_clip)

    result = await commentary_module.process_video_commentary("fake.mp4")

    assert isinstance(result, CommentaryResponse)
    assert result.quips[0].audio_url == "http://example.com/1.0.mp3"
    assert result.quips[1].text == "second quip"


@pytest.mark.anyio
async def test_job_id_forwarded_to_tts(monkeypatch, fake_video):
    captured_video_id = []

    async def fake_generate_audio_clip(quip, video_id=None):
        captured_video_id.append(video_id)
        return {"timestamp": quip["timestamp"], "text": quip["text"], "audio_url": "http://example.com/clip.mp3"}

    monkeypatch.setattr(llm_module, "analyze_window", fake_llm_returning(TWO_WINDOWS))
    monkeypatch.setattr(tts_module, "generate_audio_clip", fake_generate_audio_clip)

    await commentary_module.process_video_commentary("fake.mp4", job_id="job-123")
    assert captured_video_id == ["job-123", "job-123"]


@pytest.mark.anyio
async def test_progress_cb_receives_statuses_in_order_with_real_window_messages(monkeypatch, fake_video):
    events = []

    async def fake_progress_cb(status, progress, message=None):
        events.append((status, progress, message))

    monkeypatch.setattr(llm_module, "analyze_window", fake_llm_returning(TWO_WINDOWS))
    monkeypatch.setattr(tts_module, "generate_audio_clip", fake_tts)

    await commentary_module.process_video_commentary("fake.mp4", progress_cb=fake_progress_cb)

    statuses = [e[0] for e in events]
    assert "completed" not in statuses
    assert statuses.index("extracting") < statuses.index("analyzing") < statuses.index("synthesizing")
    analyzing = [e for e in events if e[0] == "analyzing"]
    assert analyzing[0][2] == "Analyzing part 1 of 2"
    assert analyzing[1][2].startswith("Analyzing part 2 of 2")
    assert analyzing[0][1] < analyzing[1][1] < 70


@pytest.mark.anyio
async def test_progress_cb_raises_does_not_break_pipeline(monkeypatch, fake_video):
    async def fake_progress_cb(status, progress, message=None):
        if status == "extracting":
            raise RuntimeError("Callback failed")

    monkeypatch.setattr(llm_module, "analyze_window", fake_llm_returning(TWO_WINDOWS))
    monkeypatch.setattr(tts_module, "generate_audio_clip", fake_tts)

    result = await commentary_module.process_video_commentary("fake.mp4", progress_cb=fake_progress_cb)
    assert isinstance(result, CommentaryResponse)
    assert len(result.quips) == 2
    assert result.quips[0].audio_url == "http://example.com/clip.mp3"


@pytest.mark.anyio
async def test_one_failing_tts_call_leaves_audio_url_none(monkeypatch, fake_video):
    call_count = 0

    async def fake_generate_audio_clip(quip, video_id=None):
        nonlocal call_count
        call_count += 1
        if quip["text"] == "second quip":
            raise RuntimeError("TTS failed")
        return {"timestamp": quip["timestamp"], "text": quip["text"], "audio_url": f"http://example.com/{quip['timestamp']}.mp3"}

    monkeypatch.setattr(llm_module, "analyze_window", fake_llm_returning(TWO_WINDOWS))
    monkeypatch.setattr(tts_module, "generate_audio_clip", fake_generate_audio_clip)

    result = await commentary_module.process_video_commentary("fake.mp4")
    assert result.quips[0].audio_url == "http://example.com/1.0.mp3"
    assert result.quips[1].audio_url is None


@pytest.mark.anyio
async def test_zero_frames_raises_valueerror_and_skips_llm(monkeypatch, fake_video):
    llm_called = False

    async def fake_analyze(*args, **kwargs):
        nonlocal llm_called
        llm_called = True
        return []

    monkeypatch.setattr(commentary_module.video_processor_service, "sample_window_frames", lambda *a, **k: [])
    monkeypatch.setattr(llm_module, "analyze_window", fake_analyze)

    with pytest.raises(ValueError, match="No frames could be extracted"):
        await commentary_module.process_video_commentary("fake.mp4")

    assert not llm_called


@pytest.mark.anyio
async def test_generate_audio_clip_rejects_invalid_video_id(monkeypatch):
    with pytest.raises(ValueError):
        await tts_module.generate_audio_clip({"timestamp": 0.0, "text": "test"}, video_id="../evil")
