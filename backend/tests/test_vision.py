import base64
import logging
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app.config import settings
from app.services import commentary as commentary_module
from app.services import llm as llm_module
from app.services import tts as tts_module
from app.services import video_processor as vp
from app.services.jobs import job_store
from app.services import pipeline as pipeline_module

W, H, FPS, SECONDS = 320, 180, 10, 6


def write_video(path, painter):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), float(FPS), (W, H))
    for i in range(FPS * SECONDS):
        frame = np.full((H, W, 3), 60, dtype=np.uint8)
        painter(frame, i)
        writer.write(frame)
    writer.release()
    return str(path)


@pytest.fixture
def moving_video(tmp_path):
    """Bright square sweeping back and forth inside the right-hand part of the frame."""
    def paint(frame, i):
        x = 190 + int(60 * abs(((i % 20) - 10) / 10))
        frame[50:90, x:x + 40] = 255
    return write_video(tmp_path / "move.avi", paint)


@pytest.fixture
def still_video(tmp_path):
    return write_video(tmp_path / "still.avi", lambda frame, i: None)


@pytest.fixture
def everywhere_video(tmp_path):
    def paint(frame, i):  # whole-frame flicker: everything moves
        frame[:] = 80 if i % 2 else 200
    return write_video(tmp_path / "flicker.avi", paint)


# ---- crop box -------------------------------------------------------------------------------

def test_crop_box_found_on_moving_subject_and_logged(moving_video, caplog):
    with caplog.at_level(logging.INFO, logger="maneframe.video"):
        box = vp.compute_crop_box(moving_video, SECONDS)
    assert box is not None
    x0, y0, x1, y1 = box
    assert (x1 - x0) * (y1 - y0) <= 0.8
    # square lives at x in [190, 290]/320, y in [50, 90]/180
    assert x0 <= 190 / W + 0.05 and x1 >= 290 / W - 0.05
    assert y0 <= 50 / H + 0.05 and y1 >= 90 / H - 0.05
    assert x0 > 0.3
    assert "Crop box" in caplog.text


def test_crop_box_none_when_no_motion(still_video):
    assert vp.compute_crop_box(still_video, SECONDS) is None


def test_crop_box_none_when_motion_everywhere(everywhere_video):
    assert vp.compute_crop_box(everywhere_video, SECONDS) is None


def test_crop_box_unreadable_video_raises(tmp_path):
    bad = tmp_path / "bad.avi"
    bad.write_bytes(b"nope")
    with pytest.raises(ValueError):
        vp.compute_crop_box(str(bad), 5.0)


# ---- windowing ------------------------------------------------------------------------------

def test_plan_windows_math():
    assert vp.plan_windows(10, 10) == [(0, 10)]
    assert vp.plan_windows(25, 10) == [(0, 10), (10, 20), (20, 25)]
    assert vp.plan_windows(21, 10) == [(0, 10), (10, 21)]  # 1s tail joins the previous window
    assert vp.plan_windows(3, 10) == [(0, 3)]
    assert vp.plan_windows(0, 10) == []


def test_frame_count_for_window():
    assert vp.frame_count_for_window(0, 10, 6) == 6
    assert vp.frame_count_for_window(0, 3, 6) == 3
    assert vp.frame_count_for_window(0, 0.2, 6) == 1


def test_sample_window_frames_even_spacing_width_and_shared_box(moving_video):
    box = (0.5, 0.25, 1.0, 0.75)
    frames = vp.sample_window_frames(moving_video, 0.0, 6.0, 6, 128, box)
    assert [f["timestamp"] for f in frames] == [0.5, 1.5, 2.5, 3.5, 4.5, 5.5]
    for f in frames:
        img = cv2.imdecode(np.frombuffer(base64.b64decode(f["image_data"]), np.uint8), cv2.IMREAD_COLOR)
        assert img.shape[1] == 128
        assert img.shape[0] == int(90 * 128 / 160)  # crop is 160x90, aspect preserved


def test_sample_window_frames_without_box_keeps_full_frame_aspect(still_video):
    frames = vp.sample_window_frames(still_video, 0.0, 2.0, 6, 160, None)
    assert len(frames) == 2
    img = cv2.imdecode(np.frombuffer(base64.b64decode(frames[0]["image_data"]), np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (90, 160)


# ---- request structure ----------------------------------------------------------------------

class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(prompt_tokens=1234, completion_tokens=56),
        )


GOOD = '{"seen": "dancers", "quips": [{"t": 2.0, "text": "Big leap!"}, {"t": 1.0, "text": "Here we go"}]}'
FRAMES = [{"timestamp": 0.5 + i, "image_data": "QUJD" * 50} for i in range(3)]


@pytest.fixture
def prompt_file(tmp_path, monkeypatch):
    path = tmp_path / "prompt.txt"
    path.write_text("You are a commentator. Write at most MAX_QUIPS quips.", encoding="utf-8")
    glossary = tmp_path / "glossary.txt"
    glossary.write_text("DRAFT: owner to review", encoding="utf-8")
    examples = tmp_path / "examples.txt"
    examples.write_text("# format comment only", encoding="utf-8")
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "hype.txt").write_text("Be loud.", encoding="utf-8")
    monkeypatch.setattr(llm_module, "PROMPT_PATH", path)
    monkeypatch.setattr(llm_module, "GLOSSARY_PATH", glossary)
    monkeypatch.setattr(llm_module, "EXAMPLES_PATH", examples)
    monkeypatch.setattr(llm_module, "STYLES_DIR", styles)
    llm_module.clear_prompt_cache()
    yield path
    llm_module.clear_prompt_cache()


def use_client(monkeypatch, replies):
    client = FakeClient(replies)
    monkeypatch.setattr(llm_module, "llm_client", client)
    return client


@pytest.mark.anyio
async def test_request_structure(monkeypatch, prompt_file):
    client = use_client(monkeypatch, [GOOD])
    quips = (await llm_module.analyze_window(FRAMES, 0.0, 3.0, ["old one", "old two"])).quips

    assert [q["text"] for q in quips] == ["Here we go", "Big leap!"]  # sorted by time
    (call,) = client.calls
    system, user = call["messages"]
    assert system["role"] == "system"
    assert system["content"] == (
        f"You are a commentator. Write at most {settings.MAX_QUIPS_PER_WINDOW} quips."
        "\n\nCOMMENTARY STYLE FOR THIS RUN\nBe loud."
    )
    parts = user["content"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == len(FRAMES)
    assert all(p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in images)
    texts = [p["text"] for p in parts if p["type"] == "text"]
    assert all("base64" not in t and "QUJD" not in t for t in texts)
    assert texts[1:] == [f"Frame at t={f['timestamp']}s:" for f in FRAMES]
    assert "old one" in texts[0] and "old two" in texts[0]
    for i, f in enumerate(FRAMES):  # label part directly before its image, images are last
        idx = 1 + 2 * i + 1
        assert parts[idx] is images[i]
        assert parts[idx - 1] == {"type": "text", "text": f"Frame at t={f['timestamp']}s:"}
    assert parts[-1]["type"] == "image_url"
    assert call["max_tokens"] == 500
    assert call["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.anyio
async def test_thinking_setting_and_think_blocks(monkeypatch, prompt_file):
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", True)
    client = use_client(monkeypatch, ["<think>hmm {not json}</think>\n" + GOOD])
    quips = (await llm_module.analyze_window(FRAMES, 0.0, 3.0)).quips
    assert len(quips) == 2
    assert client.calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


def test_prompt_placeholder_only_replaces_exact_token(monkeypatch, prompt_file):
    prompt_file.write_text("MAX_QUIPS and MAX_QUIPS_PER_WINDOW", encoding="utf-8")
    llm_module.clear_prompt_cache()
    monkeypatch.setattr(settings, "MAX_QUIPS_PER_WINDOW", 7)
    assert llm_module.load_system_prompt().startswith("7 and MAX_QUIPS_PER_WINDOW")


def test_missing_prompt_file_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_module, "PROMPT_PATH", tmp_path / "missing.txt")
    llm_module.clear_prompt_cache()
    with pytest.raises(FileNotFoundError):
        llm_module.load_system_prompt()


# ---- parsing --------------------------------------------------------------------------------

@pytest.mark.parametrize("reply", [
    "```json\n" + GOOD + "\n```",
    "Sure! Here is the result:\n" + GOOD + "\nHope that helps.",
    "<think>plan</think>" + GOOD,
    "I will think first.</think>" + GOOD,
])
def test_parse_tolerant(reply):
    assert llm_module.parse_model_json(reply)["seen"] == "dancers"


def test_parse_rejects_garbage():
    with pytest.raises(llm_module.LLMOutputError):
        llm_module.parse_model_json("no json here")


def test_clean_quips_drops_malformed_and_empty():
    parsed = {"quips": [{"t": "3", "text": " hi  there "}, {"t": 1, "text": ""}, {"text": "no t"}, "str", {"t": "x", "text": "bad"}]}
    assert llm_module.clean_quips(parsed) == [{"timestamp": 3.0, "text": "hi there"}]
    with pytest.raises(llm_module.LLMOutputError):
        llm_module.clean_quips({"quips": "nope"})


SEEN = '{"seen": "The blue lion on the left crouches."}'
QUIPS_OBJ = '{"quips": [{"t": 2.0, "text": "Big leap!"}]}'
BARE = '{"t": 3.0, "text": "Second"}\n{"t": 1.0, "text": "First"}'


@pytest.mark.parametrize("reply, texts", [
    (SEEN + "\n" + QUIPS_OBJ, ["Big leap!"]),
    (SEEN + "\n" + BARE, ["First", "Second"]),
    ("```json\n" + SEEN + "\n```\n```json\n" + QUIPS_OBJ + "\n```", ["Big leap!"]),
    ("```json\n" + SEEN + "\n" + BARE + "\n```", ["First", "Second"]),
    ("<think>{\"quips\": []}</think>" + SEEN + " and then " + QUIPS_OBJ, ["Big leap!"]),
    (SEEN + QUIPS_OBJ + BARE, ["Big leap!"]),  # a real quips list wins over bare objects
])
def test_extract_quips_from_multi_object_reply(reply, texts):
    quips = llm_module.extract_quips(llm_module.parse_model_objects(reply))
    assert [q["text"] for q in quips] == texts


@pytest.mark.parametrize("reply", [SEEN, "```json\n" + SEEN + "\n```", SEEN + '{"t": 1, "text": ""}', "no json"])
def test_extract_quips_fails_without_usable_content(reply):
    with pytest.raises(llm_module.LLMOutputError):
        llm_module.extract_quips(llm_module.parse_model_objects(reply))


@pytest.mark.anyio
async def test_multi_object_reply_needs_no_retry(monkeypatch, prompt_file):
    client = use_client(monkeypatch, [SEEN + "\n" + BARE])
    quips = (await llm_module.analyze_window(FRAMES, 0.0, 3.0)).quips
    assert [q["text"] for q in quips] == ["First", "Second"] and len(client.calls) == 1


@pytest.mark.anyio
async def test_unusable_reply_logged_in_full_capped(monkeypatch, prompt_file, caplog):
    long_reply = SEEN[:-1] + ', "pad": "' + "x" * 3000 + '"}'
    use_client(monkeypatch, [long_reply, SEEN])
    with caplog.at_level(logging.WARNING, logger="maneframe.llm"):
        with pytest.raises(llm_module.LLMOutputError):
            await llm_module.analyze_window(FRAMES, 0.0, 3.0)
    first = [r.getMessage() for r in caplog.records if "attempt 1" in r.getMessage()][0]
    assert "The blue lion" in first and "x" * 1900 in first and "x" * 2100 not in first
    assert any("attempt 2" in r.getMessage() and SEEN[:20] in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_retry_once_with_json_nudge(monkeypatch, prompt_file):
    client = use_client(monkeypatch, ["totally not json", GOOD])
    quips = (await llm_module.analyze_window(FRAMES, 0.0, 3.0)).quips
    assert len(quips) == 2 and len(client.calls) == 2
    assert "Return only the JSON" in client.calls[1]["messages"][1]["content"][0]["text"]
    assert "Return only the JSON" not in client.calls[0]["messages"][1]["content"][0]["text"]


@pytest.mark.anyio
async def test_two_bad_replies_raise(monkeypatch, prompt_file):
    client = use_client(monkeypatch, ["nope", "still nope"])
    with pytest.raises(llm_module.LLMOutputError):
        await llm_module.analyze_window(FRAMES, 0.0, 3.0)
    assert len(client.calls) == 2


@pytest.mark.anyio
async def test_transport_error_propagates_without_retry(monkeypatch, prompt_file):
    client = use_client(monkeypatch, [TimeoutError("slow"), GOOD])
    with pytest.raises(TimeoutError):
        await llm_module.analyze_window(FRAMES, 0.0, 3.0)
    assert len(client.calls) == 1


@pytest.mark.anyio
async def test_logs_tokens_and_seconds_without_base64(monkeypatch, prompt_file, caplog):
    use_client(monkeypatch, [GOOD])
    with caplog.at_level(logging.DEBUG, logger="maneframe.llm"):
        await llm_module.analyze_window(FRAMES, 0.0, 3.0, label="window 1/1")
    assert "frames=3" in caplog.text and "prompt_tokens=1234" in caplog.text
    assert "completion_tokens=56" in caplog.text and "seconds=" in caplog.text
    assert "QUJD" not in caplog.text


# ---- budget rules ---------------------------------------------------------------------------

def q(t, text):
    return {"timestamp": t, "text": text}


def words(n):
    return " ".join(f"w{i}" for i in range(n))


def texts_at(out):
    return [(o["timestamp"], o["text"]) for o in out]


def test_finalize_sorts_clamps_and_drops_close_quips():
    out = commentary_module.finalize_timeline(
        [q(8.0, "late one"), q(-3.0, "early"), q(2.0, "too close"), q(50.0, "past the end"), q(0.5, "x")], duration=10.0
    )
    assert texts_at(out) == [(0.0, "early"), (8.0, "late one")]


def test_finalize_gap_is_inclusive_and_far_quips_clamp_to_duration():
    out = commentary_module.finalize_timeline([q(0.0, "a b c"), q(4.4, "dropped"), q(4.5, "kept"), q(99.0, "clamped")], 10.0)
    assert texts_at(out) == [(0.0, "a b c"), (4.5, "kept"), (10.0, "clamped")]


def test_finalize_never_cuts_text_and_rounds_timestamps():
    long = words(30)
    out = commentary_module.finalize_timeline([q(0.123456, long), q(5.0, words(20))], 20.0)
    assert out[0]["timestamp"] == 0.12
    assert [o["text"] for o in out] == [long, words(20)]


def test_finalize_drops_empty_text():
    assert commentary_module.finalize_timeline([q(0.0, "   ")], 10.0) == []


# ---- pipeline: merge, previous quips, failure -----------------------------------------------

@pytest.fixture
def two_windows(monkeypatch):
    monkeypatch.setattr(settings, "MIN_QUIP_GAP_SEC", 2.5)
    vp_ = commentary_module.video_processor_service
    monkeypatch.setattr(vp_, "get_video_duration", lambda p: 20.0)
    monkeypatch.setattr(vp_, "compute_crop_box", lambda p, d: None)
    monkeypatch.setattr(vp_, "sample_window_frames", lambda p, s, e, n, w, box=None: [{"timestamp": s, "image_data": "abc"}])

    async def ok_tts(quip, video_id=None):
        return {**quip, "audio_url": "http://x/a.mp3"}

    monkeypatch.setattr(tts_module, "generate_audio_clip", ok_tts)


@pytest.mark.anyio
async def test_merge_enforces_gap_across_windows_and_passes_last_two_quips(monkeypatch, two_windows):
    seen_previous = []
    replies = [
        [q(1.0, "one"), q(4.0, "two"), q(9.0, "three")],
        [q(10.5, "too soon"), q(14.0, "fine")],
    ]

    async def fake_analyze(frames, start, end, previous=None, label="", **_):
        seen_previous.append(list(previous))
        return llm_module.WindowResult(replies[len(seen_previous) - 1])

    monkeypatch.setattr(llm_module, "analyze_window", fake_analyze)
    result = await commentary_module.process_video_commentary("v.mp4")

    assert seen_previous == [[], ["two", "three"]]
    assert [(x.timestamp, x.text) for x in result.quips] == [(1.0, "one"), (4.0, "two"), (9.0, "three"), (14.0, "fine")]


@pytest.mark.anyio
async def test_failed_window_is_skipped_and_reported(monkeypatch, two_windows, caplog):
    calls = []

    async def flaky(frames, start, end, previous=None, label="", **_):
        calls.append(start)
        if start > 0:
            raise llm_module.LLMOutputError("window 2 gave garbage")
        return llm_module.WindowResult([q(1.0, "one")])

    monkeypatch.setattr(llm_module, "analyze_window", flaky)
    with caplog.at_level(logging.WARNING, logger="maneframe.commentary"):
        result = await commentary_module.process_video_commentary("v.mp4")

    assert [x.text for x in result.quips] == ["one"] and result.quips[0].audio_url
    assert [(r.start, r.end) for r in result.skipped] == [(10.0, 20.0)]
    assert result.total_windows == 2
    assert calls == [0.0, 10.0]  # LLMOutputError was already retried inside analyze_window
    assert "Skipped 1 of 2 windows" in caplog.text


@pytest.mark.anyio
async def test_transport_failure_gets_one_retry_then_succeeds(monkeypatch, two_windows):
    calls = []

    async def flaky(frames, start, end, previous=None, label="", **_):
        calls.append(start)
        if start > 0 and calls.count(start) == 1:
            raise TimeoutError("slow")
        return llm_module.WindowResult([q(start + 1.0, f"line {int(start)}")])

    monkeypatch.setattr(llm_module, "analyze_window", flaky)
    result = await commentary_module.process_video_commentary("v.mp4")
    assert calls == [0.0, 10.0, 10.0]
    assert result.skipped == [] and len(result.quips) == 2


@pytest.mark.anyio
async def test_transport_failure_twice_skips_window(monkeypatch, two_windows):
    calls = []

    async def flaky(frames, start, end, previous=None, label="", **_):
        calls.append(start)
        if start > 0:
            raise TimeoutError("slow")
        return llm_module.WindowResult([q(1.0, "one")])

    monkeypatch.setattr(llm_module, "analyze_window", flaky)
    result = await commentary_module.process_video_commentary("v.mp4")
    assert calls == [0.0, 10.0, 10.0]
    assert [(r.start, r.end) for r in result.skipped] == [(10.0, 20.0)]


@pytest.mark.anyio
async def test_every_window_failing_still_raises_and_skips_tts(monkeypatch, two_windows):
    tts_calls = []

    async def spy_tts(quip, video_id=None):
        tts_calls.append(quip)
        return {**quip, "audio_url": None}

    async def always_garbage(frames, start, end, previous=None, label="", **_):
        raise llm_module.LLMOutputError(f"{label} gave garbage")

    monkeypatch.setattr(tts_module, "generate_audio_clip", spy_tts)
    monkeypatch.setattr(llm_module, "analyze_window", always_garbage)
    with pytest.raises(llm_module.LLMOutputError):
        await commentary_module.process_video_commentary("v.mp4")
    assert tts_calls == []


def _set_windows(monkeypatch, count):
    monkeypatch.setattr(commentary_module.video_processor_service, "get_video_duration", lambda p: count * 10.0)


def _fail_windows(monkeypatch, failing, error, calls):
    async def fake(frames, start, end, previous=None, label="", **_):
        index = int(start // 10)
        calls.append(index)
        if index in failing:
            raise error
        return llm_module.WindowResult([q(start + 1.0, f"line {index}")])

    monkeypatch.setattr(llm_module, "analyze_window", fake)


@pytest.mark.anyio
async def test_unreachable_server_fails_the_job_immediately(monkeypatch, two_windows):
    import httpx
    import openai

    calls = []
    refused = openai.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions"))
    _fail_windows(monkeypatch, {1}, refused, calls)
    with pytest.raises(openai.APIConnectionError):
        await commentary_module.process_video_commentary("v.mp4")
    assert calls == [0, 1]  # no retry, no skip


@pytest.mark.anyio
async def test_builtin_connection_error_also_fails_immediately(monkeypatch, two_windows):
    calls = []
    _fail_windows(monkeypatch, {0}, ConnectionRefusedError("refused"), calls)
    with pytest.raises(ConnectionRefusedError):
        await commentary_module.process_video_commentary("v.mp4")
    assert calls == [0]


def test_is_unreachable_distinguishes_timeout_from_refused():
    import httpx
    import openai

    request = httpx.Request("POST", "http://x")
    assert commentary_module.is_unreachable(openai.APIConnectionError(request=request))
    assert not commentary_module.is_unreachable(openai.APITimeoutError(request=request))
    assert not commentary_module.is_unreachable(TimeoutError("slow"))
    assert not commentary_module.is_unreachable(llm_module.LLMOutputError("garbage"))


@pytest.mark.anyio
async def test_more_than_half_skipped_fails_the_job(monkeypatch, two_windows):
    calls = []
    _set_windows(monkeypatch, 5)
    _fail_windows(monkeypatch, {0, 2, 4}, llm_module.LLMOutputError("garbage"), calls)  # never 3 in a row
    with pytest.raises(llm_module.LLMOutputError):
        await commentary_module.process_video_commentary("v.mp4")
    assert calls == [0, 1, 2, 3, 4]


@pytest.mark.anyio
async def test_exactly_half_skipped_still_completes(monkeypatch, two_windows):
    calls = []
    _set_windows(monkeypatch, 4)
    _fail_windows(monkeypatch, {0, 2}, llm_module.LLMOutputError("garbage"), calls)
    result = await commentary_module.process_video_commentary("v.mp4")
    assert [(r.start, r.end) for r in result.skipped] == [(0.0, 10.0), (20.0, 30.0)]


@pytest.mark.anyio
async def test_three_skips_in_a_row_fail_the_job_early(monkeypatch, two_windows):
    calls = []
    _set_windows(monkeypatch, 10)
    _fail_windows(monkeypatch, {3, 4, 5}, llm_module.LLMOutputError("garbage"), calls)
    with pytest.raises(llm_module.LLMOutputError):
        await commentary_module.process_video_commentary("v.mp4")
    assert calls == [0, 1, 2, 3, 4, 5]  # stopped at the third, windows 6-9 never run


@pytest.mark.anyio
async def test_a_success_resets_the_consecutive_skip_count(monkeypatch, two_windows):
    calls = []
    _set_windows(monkeypatch, 10)
    _fail_windows(monkeypatch, {1, 2, 4, 5}, llm_module.LLMOutputError("garbage"), calls)
    result = await commentary_module.process_video_commentary("v.mp4")
    assert len(result.skipped) == 4 and result.total_windows == 10


@pytest.mark.anyio
async def test_partial_failure_job_completes_with_skipped_count_in_message(monkeypatch, two_windows, tmp_path):
    async def flaky(frames, start, end, previous=None, label="", **_):
        if start > 0:
            raise llm_module.LLMOutputError("garbage")
        return llm_module.WindowResult([q(1.0, "one")])

    monkeypatch.setattr(llm_module, "analyze_window", flaky)
    video = tmp_path / "v.avi"
    video.write_bytes(b"x")
    job_store.create(str(video), job_id="partial")
    await pipeline_module.run_pipeline("partial", str(video))

    job = job_store.get("partial")
    assert job.status == "completed"
    assert job.message == "Commentary ready, skipped 1 of 2 parts"
    assert job.result.skipped[0].start == 10.0


@pytest.mark.anyio
async def test_no_quips_at_all_is_an_error(monkeypatch, two_windows):
    async def empty(frames, start, end, previous=None, label="", **_):
        return llm_module.WindowResult([])

    monkeypatch.setattr(llm_module, "analyze_window", empty)
    with pytest.raises(llm_module.LLMOutputError, match="no usable commentary"):
        await commentary_module.process_video_commentary("v.mp4")


@pytest.mark.anyio
async def test_failed_window_ends_job_in_error_with_generic_message_and_logs_cause(monkeypatch, two_windows, tmp_path, caplog):
    async def boom(frames, start, end, previous=None, label="", **_):
        raise RuntimeError("secret connection refused detail")

    monkeypatch.setattr(llm_module, "analyze_window", boom)
    video = tmp_path / "v.avi"
    video.write_bytes(b"x")
    job_store.create(str(video), job_id="visfail")
    with caplog.at_level(logging.ERROR):
        await pipeline_module.run_pipeline("visfail", str(video))

    job = job_store.get("visfail")
    assert job.status == "error"
    assert job.message == "Commentary generation failed"
    assert "secret" not in str(job.message)
    assert "secret connection refused detail" in caplog.text
    assert not video.exists()


@pytest.mark.anyio
async def test_total_pipeline_time_is_logged(monkeypatch, two_windows, caplog):
    async def fake_analyze(frames, start, end, previous=None, label="", **_):
        return llm_module.WindowResult([q(start + 1.0, "hello there")])

    monkeypatch.setattr(llm_module, "analyze_window", fake_analyze)
    with caplog.at_level(logging.INFO, logger="maneframe.commentary"):
        await commentary_module.process_video_commentary("v.mp4", job_id="timed")
    assert "finished in" in caplog.text


def http_error(status):
    import httpx
    import openai

    response = httpx.Response(status, request=httpx.Request("POST", "http://llm/v1/chat/completions"))
    return openai.APIStatusError("boom", response=response, body=None)


@pytest.mark.anyio
async def test_http_400_with_schema_retries_once_without_response_format(monkeypatch, prompt_file, caplog):
    monkeypatch.setattr(settings, "LLM_JSON_SCHEMA", True)
    client = use_client(monkeypatch, [http_error(400), GOOD])
    with caplog.at_level(logging.WARNING, logger="maneframe.llm"):
        quips = (await llm_module.analyze_window(FRAMES, 0.0, 3.0)).quips
    assert len(quips) == 2 and len(client.calls) == 2
    assert "response_format" in client.calls[0] and "response_format" not in client.calls[1]
    assert any("HTTP 400" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.anyio
async def test_http_400_without_schema_is_not_retried(monkeypatch, prompt_file):
    monkeypatch.setattr(settings, "LLM_JSON_SCHEMA", False)
    client = use_client(monkeypatch, [http_error(400), GOOD])
    with pytest.raises(Exception) as info:
        await llm_module.analyze_window(FRAMES, 0.0, 3.0)
    assert info.value.status_code == 400 and len(client.calls) == 1


@pytest.mark.anyio
async def test_other_http_errors_with_schema_are_not_retried(monkeypatch, prompt_file):
    monkeypatch.setattr(settings, "LLM_JSON_SCHEMA", True)
    client = use_client(monkeypatch, [http_error(500), GOOD])
    with pytest.raises(Exception) as info:
        await llm_module.analyze_window(FRAMES, 0.0, 3.0)
    assert info.value.status_code == 500 and len(client.calls) == 1


@pytest.mark.anyio
async def test_second_400_propagates(monkeypatch, prompt_file):
    monkeypatch.setattr(settings, "LLM_JSON_SCHEMA", True)
    client = use_client(monkeypatch, [http_error(400), http_error(400)])
    with pytest.raises(Exception) as info:
        await llm_module.analyze_window(FRAMES, 0.0, 3.0)
    assert info.value.status_code == 400 and len(client.calls) == 2


def test_llm_client_does_not_retry_on_its_own():
    assert llm_module.llm_client.max_retries == 0


@pytest.mark.anyio
async def test_json_schema_setting_controls_response_format(monkeypatch, prompt_file):
    monkeypatch.setattr(settings, "LLM_JSON_SCHEMA", False)
    client = use_client(monkeypatch, [GOOD, GOOD])
    await llm_module.analyze_window(FRAMES, 0.0, 3.0)
    assert "response_format" not in client.calls[0]
    monkeypatch.setattr(settings, "LLM_JSON_SCHEMA", True)
    await llm_module.analyze_window(FRAMES, 0.0, 3.0)
    schema = client.calls[1]["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["seen", "quips"]
    assert schema["properties"]["quips"]["maxItems"] == settings.MAX_QUIPS_PER_WINDOW
    assert schema["properties"]["quips"]["items"]["required"] == ["t", "text"]
