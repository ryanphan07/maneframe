import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, job_result
from app.schemas.events import CommentaryPartial
from app.services import commentary as commentary_module
from app.services import llm as llm_module
from app.services import pipeline as pipeline_module
from app.services import tts as tts_module
from app.services.jobs import job_store


def q(t, text):
    return {"timestamp": t, "text": text}


@pytest.fixture
def three_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MIN_QUIP_GAP_SEC", 2.5)
    monkeypatch.setattr(settings, "TTS_START_STAGGER_SECONDS", 0)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    vp_ = commentary_module.video_processor_service
    monkeypatch.setattr(vp_, "get_video_duration", lambda p: 30.0)
    monkeypatch.setattr(vp_, "compute_crop_box", lambda p, d: None)
    monkeypatch.setattr(vp_, "sample_window_frames", lambda p, s, e, n, w, box=None: [{"timestamp": s, "image_data": "abc"}])

    async def no_prepare(path, progress_cb):
        return pipeline_module.PreparedVideo(30.0, "video/x-msvideo")

    monkeypatch.setattr(pipeline_module, "prepare_video", no_prepare)

    async def tts(quip, video_id=None):
        # 3 s clips, none for the text "mute"; the url is derived from the quip so runs can be compared
        if quip["text"] == "mute":
            return None
        await asyncio.sleep(0.01)
        return {**quip, "audio_url": f"http://x/{quip['timestamp']}_{quip['text']}.mp3", "duration_sec": 3.0}

    monkeypatch.setattr(tts_module, "generate_audio_clip", tts)
    monkeypatch.setattr(tts_module, "remove_clip_file", lambda video_id, url: None)


def set_replies(monkeypatch, replies):
    async def analyze(frames, start, end, previous=None, label="", **_):
        reply = replies[int(start // 10)]
        if isinstance(reply, Exception):
            raise reply
        return llm_module.WindowResult(reply)

    monkeypatch.setattr(llm_module, "analyze_window", analyze)


def old_flow(replies, duration=30.0):
    """The previous all-at-end flow: finalize everything, then schedule."""
    raw = [item for r in replies if not isinstance(r, Exception) for item in r]
    timeline = commentary_module.finalize_timeline(raw, duration)
    with_audio = [
        {**item, "audio_url": None if item["text"] == "mute" else f"http://x/{item['timestamp']}_{item['text']}.mp3",
         "duration_sec": None if item["text"] == "mute" else 3.0}
        for item in timeline
    ]
    return commentary_module.schedule_quips(with_audio, duration)


@pytest.mark.anyio
@pytest.mark.parametrize("replies", [
    [[q(1, "a"), q(5, "b")], [q(11, "c"), q(12, "mute"), q(16, "d")], [q(21, "e"), q(24, "f")]],
    # overlapping audio gets shifted or dropped across a window boundary
    [[q(1, "a"), q(8, "b"), q(9.6, "c")], [q(10.2, "d"), q(11, "e")], [q(21, "f")]],
    # a window returns timestamps inside an earlier window's range, so the early timeline changes
    [[q(1, "a"), q(8, "b")], [q(2.5, "late"), q(15, "c")], [q(25, "d")]],
])
async def test_progressive_result_equals_the_all_at_end_flow(monkeypatch, three_windows, replies):
    set_replies(monkeypatch, replies)
    result = await commentary_module.process_video_commentary("v.mp4")
    assert [x.model_dump() for x in result.quips] == old_flow(replies)


@pytest.mark.anyio
async def test_partials_grow_per_window_and_carry_audio(monkeypatch, three_windows):
    set_replies(monkeypatch, [[q(1, "a")], [q(12, "b")], [q(22, "c")]])
    partials = []
    result = await commentary_module.process_video_commentary("v.mp4", partial_cb=partials.append)
    assert partials, "partial_cb was never called"
    assert [p.windows_done for p in partials] == sorted(p.windows_done for p in partials)
    assert all(p.total_windows == 3 for p in partials)
    first = next(p for p in partials if p.windows_done == 1)
    assert first.ready_until == 10.0
    assert [(x.timestamp, x.text) for x in first.quips] == [(1.0, "a")]
    assert first.quips[0].audio_url == "http://x/1_a.mp3"
    last = partials[-1]
    assert last.windows_done == 3 and last.ready_until == 30.0
    assert [x.model_dump() for x in last.quips] == [x.model_dump() for x in result.quips]


@pytest.mark.anyio
async def test_skipped_window_counts_as_done_in_partial(monkeypatch, three_windows):
    set_replies(monkeypatch, [[q(1, "a")], llm_module.LLMOutputError("garbage"), [q(22, "c")]])
    partials = []
    result = await commentary_module.process_video_commentary("v.mp4", partial_cb=partials.append)
    assert [(r.start, r.end) for r in result.skipped] == [(10.0, 20.0)]
    assert any(p.windows_done == 2 and [x.text for x in p.quips] == ["a"] for p in partials)
    assert partials[-1].windows_done == 3


@pytest.mark.anyio
async def test_a_failing_partial_callback_does_not_break_the_job(monkeypatch, three_windows):
    set_replies(monkeypatch, [[q(1, "a")], [q(12, "b")], [q(22, "c")]])

    def bad(partial):
        raise RuntimeError("boom")

    result = await commentary_module.process_video_commentary("v.mp4", partial_cb=bad)
    assert len(result.quips) == 3


@pytest.mark.anyio
async def test_job_exposes_partial_while_running_and_final_result_has_none(monkeypatch, three_windows, tmp_path):
    seen = {}

    async def analyze(frames, start, end, previous=None, label="", **_):
        if start >= 20:
            await asyncio.sleep(0.2)  # the first two windows' audio finishes while window 3 is analyzed
            job = job_store.get("pj")
            seen["status"] = job.status
            seen["result"] = job_result(job)
        return llm_module.WindowResult([q(start + 1, f"w{int(start // 10)}")])

    monkeypatch.setattr(llm_module, "analyze_window", analyze)
    video = tmp_path / "v.avi"
    video.write_bytes(b"x")
    job_store.create(str(video), job_id="pj")
    await pipeline_module.run_pipeline("pj", str(video))

    assert seen["status"] == "analyzing"
    partial = seen["result"]["partial"]
    assert set(partial) == {"quips", "windows_done", "total_windows", "ready_until"}
    assert partial["windows_done"] == 2 and partial["total_windows"] == 3
    assert [x["text"] for x in partial["quips"]] == ["w0", "w1"]
    assert all(x["audio_url"] for x in partial["quips"])
    job = job_store.get("pj")
    final = job_result(job)
    assert job.status == "completed" and "partial" not in final and len(final["quips"]) == 3


@pytest.mark.anyio
async def test_failure_mid_job_never_shows_partial_data(monkeypatch, three_windows, tmp_path):
    seen = {}

    async def analyze(frames, start, end, previous=None, label="", **_):
        if start >= 20:
            await asyncio.sleep(0.1)
            seen["during"] = job_result(job_store.get("pf"))
            raise ConnectionError("llama-server went away")
        return llm_module.WindowResult([q(start + 1, f"w{int(start // 10)}")])

    monkeypatch.setattr(llm_module, "analyze_window", analyze)
    video = tmp_path / "v.avi"
    video.write_bytes(b"x")
    job_store.create(str(video), job_id="pf")
    await pipeline_module.run_pipeline("pf", str(video))

    assert seen["during"]["partial"]["quips"], "partial data was available while the job ran"
    job = job_store.get("pf")
    assert job.status == "error" and job.result is None and job.partial is None
    assert job_result(job) is None


def test_get_job_endpoint_returns_partial_then_nothing_after_failure():
    job_store.create("v.mp4", job_id="api1")
    job_store.update("api1", "analyzing", 30)
    job_store.set_partial("api1", CommentaryPartial(quips=[], windows_done=1, total_windows=3, ready_until=10.0))
    with TestClient(app) as client:
        body = client.get("/api/jobs/api1").json()
        assert body["status"] == "analyzing" and body["result"] == {
            "partial": {"quips": [], "windows_done": 1, "total_windows": 3, "ready_until": 10.0}
        }
        job_store.fail("api1", "nope")
        body = client.get("/api/jobs/api1").json()
        assert body["status"] == "error" and body["result"] is None


@pytest.mark.anyio
async def test_a_shift_is_logged_once_across_windows(monkeypatch, three_windows, caplog):
    # c (10.6) lands inside b (8.0, 3 s long), so it is shifted; the later windows reschedule it again
    set_replies(monkeypatch, [[q(1, "a"), q(8, "b")], [q(10.6, "c")], [q(22, "d")]])
    with caplog.at_level("INFO", logger="maneframe.commentary"):
        await commentary_module.process_video_commentary("v.mp4", partial_cb=lambda p: None)
    assert caplog.text.count("Shifting quip") == 1
