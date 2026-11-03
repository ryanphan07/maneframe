import asyncio
import os
from unittest.mock import MagicMock

import aiohttp
import pytest
from edge_tts.exceptions import NoAudioReceived

from app.config import settings
from app.schemas.events import CommentaryResponse
from app.services import commentary as commentary_module
from app.services import jobs as jobs_module
from app.services import llm as llm_module
from app.services import tts as tts_module
from app.services import video_processor as vp

BIG = b"\xff\xfb\x90\x00" * 100


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "TTS_ENABLED", True)
    return tmp_path / "audio" / "job1"


def files_in(d):
    return os.listdir(d) if d.exists() else []


class FakeCommunicate:
    behavior = "ok"
    calls = []
    attempts = 0

    def __init__(self, text, voice, rate=None):
        FakeCommunicate.calls.append((text, voice, rate))

    async def save(self, path):
        b = FakeCommunicate.behavior
        if b in ("flaky", "flaky_auth", "no_audio", "forbidden"):
            FakeCommunicate.attempts += 1
            if b == "forbidden":
                raise aiohttp.ClientResponseError(MagicMock(), (), status=403)
            if FakeCommunicate.attempts == 1:
                if b == "no_audio":
                    raise NoAudioReceived("No audio was received.")
                raise ConnectionResetError("reset") if b == "flaky" else PermissionError("401")
            b = "ok"
        if b == "boom":
            with open(path, "wb") as f:
                f.write(BIG)  # partial output before the failure
            raise RuntimeError("403")
        if b == "hang":
            await asyncio.sleep(10)
        with open(path, "wb") as f:
            f.write(BIG if b == "ok" else b"x")


@pytest.fixture
def fake_edge(monkeypatch):
    FakeCommunicate.behavior = "ok"
    FakeCommunicate.calls = []
    FakeCommunicate.attempts = 0
    monkeypatch.setattr(settings, "TTS_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(tts_module, "Communicate", FakeCommunicate)
    monkeypatch.setattr(tts_module, "measure_duration", lambda p: 1.5)
    return FakeCommunicate


@pytest.mark.anyio
async def test_tts_success_returns_url_and_duration(audio_dir, fake_edge):
    out = await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1")
    assert out["audio_url"].endswith(".mp3") and "/uploads/audio/job1/" in out["audio_url"]
    assert out["duration_sec"] == 1.5
    assert len(files_in(audio_dir)) == 1
    assert fake_edge.calls == [("hello", settings.TTS_VOICE, settings.TTS_RATE)]


@pytest.mark.anyio
@pytest.mark.parametrize("behavior", ["boom", "tiny"])
async def test_tts_failure_returns_none_and_leaves_no_file(audio_dir, fake_edge, behavior):
    fake_edge.behavior = behavior
    out = await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1")
    assert out is None
    assert files_in(audio_dir) == []


@pytest.mark.anyio
async def test_tts_retries_a_transient_failure_once(audio_dir, fake_edge):
    fake_edge.behavior = "flaky"
    out = await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1")
    assert out is not None and fake_edge.attempts == 2
    assert len(files_in(audio_dir)) == 1


@pytest.mark.anyio
async def test_tts_does_not_retry_when_retries_is_zero(audio_dir, fake_edge, monkeypatch):
    monkeypatch.setattr(settings, "TTS_RETRIES", 0)
    fake_edge.behavior = "flaky"
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    assert fake_edge.attempts == 1


@pytest.mark.anyio
async def test_tts_does_not_retry_auth_errors(audio_dir, fake_edge):
    import aiohttp
    from unittest.mock import MagicMock

    for exc in (aiohttp.ClientResponseError(MagicMock(), (), status=403), aiohttp.ClientResponseError(MagicMock(), (), status=401)):
        assert tts_module._is_transient(exc) is False
    assert tts_module._is_transient(aiohttp.ClientResponseError(MagicMock(), (), status=503)) is True
    fake_edge.behavior = "flaky_auth"
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    assert fake_edge.attempts == 1


@pytest.mark.anyio
async def test_tts_retries_no_audio_received_once_then_succeeds(audio_dir, fake_edge):
    fake_edge.behavior = "no_audio"
    out = await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1")
    assert out is not None and fake_edge.attempts == 2
    assert len(files_in(audio_dir)) == 1


@pytest.mark.anyio
async def test_tts_403_is_not_retried(audio_dir, fake_edge):
    fake_edge.behavior = "forbidden"
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    assert fake_edge.attempts == 1


@pytest.mark.anyio
async def test_tts_duration_is_remeasured_once_before_failing(audio_dir, fake_edge, monkeypatch):
    monkeypatch.setattr(tts_module, "DURATION_RECHECK_DELAY_SECONDS", 0)
    results = iter([None, 2.5])
    monkeypatch.setattr(tts_module, "measure_duration", lambda p: next(results))
    out = await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1")
    assert out["duration_sec"] == 2.5


@pytest.mark.anyio
async def test_tts_unreadable_duration_is_measured_twice_and_logged(audio_dir, fake_edge, monkeypatch, caplog):
    monkeypatch.setattr(tts_module, "DURATION_RECHECK_DELAY_SECONDS", 0)
    calls = []
    monkeypatch.setattr(tts_module, "measure_duration", lambda p: calls.append(p))
    with caplog.at_level("WARNING", logger="maneframe.tts"):
        assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    assert len(calls) == 2
    assert "first4=fffb9000" in caplog.text and "file_bytes=400" in caplog.text
    assert files_in(audio_dir) == []


@pytest.mark.anyio
async def test_tts_failure_log_has_diagnostics(audio_dir, fake_edge, caplog):
    fake_edge.behavior = "boom"
    with caplog.at_level("WARNING", logger="maneframe.tts"):
        await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1")
    assert "RuntimeError" in caplog.text and "text_len=5" in caplog.text
    assert settings.TTS_VOICE in caplog.text and "file_bytes=400" in caplog.text


@pytest.mark.anyio
async def test_tts_timeout_returns_none(audio_dir, fake_edge, monkeypatch):
    monkeypatch.setattr(settings, "TTS_TIMEOUT_SECONDS", 0.05)
    fake_edge.behavior = "hang"
    assert await tts_module.generate_audio_clip({"timestamp": 0.0, "text": "hi"}, video_id="job1") is None
    assert files_in(audio_dir) == []


@pytest.mark.anyio
async def test_tts_unreadable_duration_discards_file(audio_dir, fake_edge, monkeypatch):
    monkeypatch.setattr(tts_module, "measure_duration", lambda p: None)
    assert await tts_module.generate_audio_clip({"timestamp": 0.0, "text": "hi"}, video_id="job1") is None
    assert files_in(audio_dir) == []


@pytest.mark.anyio
async def test_tts_disabled_writes_nothing(audio_dir, fake_edge, monkeypatch):
    monkeypatch.setattr(settings, "TTS_ENABLED", False)
    assert await tts_module.generate_audio_clip({"timestamp": 0.0, "text": "hi"}, video_id="job1") is None
    assert files_in(audio_dir) == []


def test_measure_duration_real_mp3(tmp_path):
    # Hand-built MPEG1 Layer III, 128kbps/44.1kHz: 417-byte frames of 26.12ms each.
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    path = tmp_path / "a.mp3"
    path.write_bytes(frame * 100)
    assert tts_module.measure_duration(str(path)) == pytest.approx(2.612, abs=0.05)


def test_measure_duration_garbage_is_none(tmp_path):
    path = tmp_path / "bad.mp3"
    path.write_bytes(b"not audio")
    assert tts_module.measure_duration(str(path)) is None


# ---- schedule_quips ----

def aq(t, dur=2.0, text="x"):
    return {"timestamp": t, "text": text, "audio_url": f"u{t}", "duration_sec": dur}


def na(t):
    return {"timestamp": t, "text": "x", "audio_url": None, "duration_sec": None}


def times(quips):
    return [q["timestamp"] for q in quips]


def test_schedule_leaves_fitting_quips_alone():
    quips = [aq(0.0), aq(3.0)]
    assert commentary_module.schedule_quips(quips) == quips


def test_schedule_shifts_overlap_by_gap():
    assert times(commentary_module.schedule_quips([aq(0.0, 3.0), aq(2.0)])) == [0.0, pytest.approx(3.3)]


def test_schedule_drops_unfittable():
    assert times(commentary_module.schedule_quips([aq(0.0, 6.0), aq(1.0), aq(7.0)])) == [0.0, 7.0]


def test_schedule_shift_chains_into_next():
    out = commentary_module.schedule_quips([aq(0.0, 3.0), aq(2.0, 2.0), aq(4.0, 1.0)])
    assert times(out) == [0.0, pytest.approx(3.3), pytest.approx(5.6)]


def test_schedule_no_audio_kept_and_never_blocks():
    out = commentary_module.schedule_quips([aq(0.0, 5.0), na(1.0), na(2.0), aq(5.5)])
    assert times(out) == [0.0, 1.0, 2.0, 5.5]
    assert out[1]["audio_url"] is None


def test_schedule_orders_input_and_does_not_mutate():
    original = [aq(5.0), aq(0.0)]
    assert times(commentary_module.schedule_quips(original)) == [0.0, 5.0]
    assert times(original) == [5.0, 0.0]


# ---- audio_available ----

@pytest.fixture
def one_window(monkeypatch):
    monkeypatch.setattr(settings, "MIN_QUIP_GAP_SEC", 2.5)
    monkeypatch.setattr(vp, "get_video_duration", lambda p: 8.0)
    monkeypatch.setattr(vp, "compute_crop_box", lambda p, d: None)
    monkeypatch.setattr(vp, "sample_window_frames", lambda p, s, e, n, w, box=None: [{"timestamp": s, "image_data": "abc"}])

    async def analyze(frames, start, end, previous=None, label="", **_):
        return llm_module.WindowResult([{"timestamp": 1.0, "text": "one two three"}, {"timestamp": 4.0, "text": "four five six"}])

    monkeypatch.setattr(llm_module, "analyze_window", analyze)


@pytest.mark.anyio
async def test_audio_available_true_and_durations_carried(monkeypatch, one_window):
    async def ok(quip, video_id=None):
        return {**quip, "audio_url": "http://x/a.mp3", "duration_sec": 1.2}

    monkeypatch.setattr(tts_module, "generate_audio_clip", ok)
    result = await commentary_module.process_video_commentary("v.mp4")
    assert result.audio_available is True
    assert [q.duration_sec for q in result.quips] == [1.2, 1.2]


@pytest.mark.anyio
async def test_audio_available_false_when_all_tts_fail(monkeypatch, one_window):
    async def none(quip, video_id=None):
        return None

    monkeypatch.setattr(tts_module, "generate_audio_clip", none)
    result = await commentary_module.process_video_commentary("v.mp4")
    assert result.audio_available is False
    assert len(result.quips) == 2
    assert all(q.audio_url is None and q.duration_sec is None for q in result.quips)


@pytest.mark.anyio
@pytest.mark.parametrize("concurrency,expected_max", [(1, 1), (2, 2)])
async def test_tts_concurrency_limits_parallel_calls(monkeypatch, one_window, concurrency, expected_max):
    monkeypatch.setattr(settings, "TTS_CONCURRENCY", concurrency)
    monkeypatch.setattr(settings, "TTS_START_STAGGER_SECONDS", 0)
    state = {"now": 0, "max": 0}

    async def slow(quip, video_id=None):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return {**quip, "audio_url": "http://x/a.mp3", "duration_sec": 1.0}

    monkeypatch.setattr(tts_module, "generate_audio_clip", slow)
    await commentary_module.process_video_commentary("v.mp4")
    assert state["max"] == expected_max


def test_job_message_without_audio():
    store = jobs_module.JobStore()
    job = store.create("video.mp4")
    store.complete(job.job_id, CommentaryResponse(quips=[], audio_available=False))
    assert job.message == "Commentary ready (audio unavailable)"


# ---- parser ----

@pytest.mark.parametrize("reply,expected", [
    ('{"quips": []}`', {"quips": []}),
    ('```json\n{"quips": [{"t": 1, "text": "hi"}]}\n```', {"quips": [{"t": 1, "text": "hi"}]}),
    ('<think>{"no": 1}</think>{"quips": []} trailing words', {"quips": []}),
    ('Sure! {"seen": "x", "quips": []}', {"seen": "x", "quips": []}),
])
def test_parse_model_json_tolerates_junk(reply, expected):
    assert llm_module.parse_model_json(reply) == expected


def test_parse_model_json_no_object_raises():
    with pytest.raises(llm_module.LLMOutputError):
        llm_module.parse_model_json("no json here")


def test_analyzing_message_estimates_time_left():
    msg = commentary_module.analyzing_message
    assert msg(0, 6, None) == "Analyzing part 1 of 6"
    assert msg(1, 6, 45.0) == "Analyzing part 2 of 6, about 4 min left"
    assert msg(5, 6, 20.0) == "Analyzing part 6 of 6, under a minute left"


def test_schedule_drops_quip_shifted_past_video_end():
    out = commentary_module.schedule_quips([aq(0.0, 4.0), aq(3.9)], video_duration=4.2)
    assert times(out) == [0.0]
    assert times(commentary_module.schedule_quips([aq(0.0, 4.0), aq(3.9)], video_duration=60.0)) == [0.0, pytest.approx(4.3)]


def test_remove_clip_file_deletes_only_mp3_in_job_folder(audio_dir):
    audio_dir.mkdir(parents=True)
    clip = audio_dir / "audio_1.00_ab12cd34.mp3"
    other = audio_dir / "keep.txt"
    clip.write_bytes(BIG)
    other.write_text("x")
    tts_module.remove_clip_file("job1", "http://localhost:8000/uploads/audio/job1/audio_1.00_ab12cd34.mp3")
    tts_module.remove_clip_file("job1", "http://x/../../keep.txt")
    tts_module.remove_clip_file("../evil", "http://x/a.mp3")
    assert not clip.exists() and other.exists()


@pytest.mark.anyio
async def test_dropped_quip_audio_file_is_deleted(monkeypatch, one_window, audio_dir):
    audio_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "MAX_SHIFT_SEC", 0.1)

    async def clip(quip, video_id=None):
        name = f"audio_{quip['timestamp']:.2f}_aaaaaaaa.mp3"
        (audio_dir / name).write_bytes(BIG)
        return {**quip, "audio_url": f"http://x/uploads/audio/job1/{name}", "duration_sec": 6.0}

    monkeypatch.setattr(tts_module, "generate_audio_clip", clip)
    result = await commentary_module.process_video_commentary("v.mp4", job_id="job1")
    assert [q.timestamp for q in result.quips] == [1.0]
    assert sorted(os.listdir(audio_dir)) == ["audio_1.00_aaaaaaaa.mp3"]


def test_schedule_logs_a_shift_once_when_given_a_logged_set(caplog):
    logged = set()
    quips = [aq(0.0, 3.0), aq(2.0)]
    with caplog.at_level("INFO", logger="maneframe.commentary"):
        for _ in range(3):
            commentary_module.schedule_quips(quips, None, logged)
    assert caplog.text.count("Shifting quip") == 1
    caplog.clear()
    with caplog.at_level("INFO", logger="maneframe.commentary"):
        commentary_module.schedule_quips(quips)
        commentary_module.schedule_quips(quips)
    assert caplog.text.count("Shifting quip") == 2  # no set: unchanged behavior
