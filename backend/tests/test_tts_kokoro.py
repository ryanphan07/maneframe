import os
import wave

import numpy as np
import pytest

from app.config import settings
from app.services import tts as tts_module

VOICES = ["af_bella", "af_nicole", "bm_george"]


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "TTS_ENABLED", True)
    monkeypatch.setattr(settings, "TTS_ENGINE", "kokoro")
    return tmp_path / "audio" / "job1"


def files_in(d):
    return os.listdir(d) if d.exists() else []


class FakeKokoro:
    behavior = "ok"
    inits = []
    calls = []

    def __init__(self, model_path, voices_path):
        FakeKokoro.inits.append((model_path, voices_path))

    def get_voices(self):
        return VOICES

    def get_voice_style(self, name):
        return np.full(4, float(VOICES.index(name) + 1), dtype=np.float32)

    def create(self, text, voice, speed=1.0, lang="en-us"):
        FakeKokoro.calls.append((text, voice, speed, lang))
        if FakeKokoro.behavior == "boom":
            raise RuntimeError("onnx failed")
        n = 0 if FakeKokoro.behavior == "empty" else 24000 * 2
        return np.full(n, 0.1, dtype=np.float32), 24000  # 2 s


@pytest.fixture
def fake_kokoro(tmp_path, monkeypatch):
    model, voices = tmp_path / "k.onnx", tmp_path / "v.bin"
    model.write_bytes(b"x")
    voices.write_bytes(b"x")
    FakeKokoro.behavior, FakeKokoro.inits, FakeKokoro.calls = "ok", [], []
    monkeypatch.setattr(settings, "KOKORO_MODEL_PATH", str(model))
    monkeypatch.setattr(settings, "KOKORO_VOICES_PATH", str(voices))
    monkeypatch.setattr(settings, "KOKORO_VOICE", "af_nicole")
    monkeypatch.setattr(settings, "KOKORO_GAIN_DB", 0.0)
    monkeypatch.setattr(tts_module, "Kokoro", FakeKokoro)
    monkeypatch.setattr(tts_module, "_kokoro_engine", None)
    monkeypatch.setattr(tts_module, "_kokoro_key", None)
    return FakeKokoro


@pytest.mark.anyio
async def test_kokoro_returns_wav_url_and_measured_duration(audio_dir, fake_kokoro):
    out = await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1")
    assert out["audio_url"].endswith(".wav") and "/uploads/audio/job1/" in out["audio_url"]
    assert out["duration_sec"] == pytest.approx(2.0)
    assert files_in(audio_dir) == [os.path.basename(out["audio_url"])]
    assert fake_kokoro.calls == [("hello", "af_nicole", 1.0, "en-us")]


@pytest.mark.anyio
async def test_kokoro_loads_once_and_passes_speed_and_lang(audio_dir, fake_kokoro, monkeypatch):
    monkeypatch.setattr(settings, "KOKORO_SPEED", 1.2)
    monkeypatch.setattr(settings, "KOKORO_LANG", "en-gb")
    for t in (1.0, 6.0):
        assert await tts_module.generate_audio_clip({"timestamp": t, "text": f"line {t}"}, video_id="job1")
    assert len(fake_kokoro.inits) == 1
    assert [c[2:] for c in fake_kokoro.calls] == [(1.2, "en-gb")] * 2


@pytest.mark.anyio
async def test_kokoro_blend_mixes_styles_by_normalized_weight(audio_dir, fake_kokoro, monkeypatch):
    monkeypatch.setattr(settings, "KOKORO_VOICE", "af_nicole:7,af_bella:3")
    assert await tts_module.generate_audio_clip({"timestamp": 1.0, "text": "hi"}, video_id="job1")
    style = fake_kokoro.calls[0][1]
    assert style == pytest.approx(np.full(4, 0.7 * 2 + 0.3 * 1))


@pytest.mark.anyio
@pytest.mark.parametrize("behavior", ["boom", "empty"])
async def test_kokoro_failure_gives_no_audio_and_no_file(audio_dir, fake_kokoro, behavior):
    fake_kokoro.behavior = behavior
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    assert files_in(audio_dir) == []


@pytest.mark.anyio
async def test_kokoro_missing_files_or_package_gives_no_audio(audio_dir, fake_kokoro, monkeypatch):
    monkeypatch.setattr(tts_module, "Kokoro", None)
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    monkeypatch.setattr(tts_module, "Kokoro", FakeKokoro)
    monkeypatch.setattr(settings, "KOKORO_MODEL_PATH", str(audio_dir / "nope.onnx"))
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    assert files_in(audio_dir) == []


@pytest.mark.anyio
async def test_kokoro_unknown_voice_at_synthesis_gives_no_audio(audio_dir, fake_kokoro, monkeypatch):
    monkeypatch.setattr(settings, "KOKORO_VOICE", "af_nope")
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    assert files_in(audio_dir) == []


def test_parse_voice_single_and_blend():
    assert tts_module.parse_kokoro_voice("af_nicole") == [("af_nicole", 1.0)]
    parsed = tts_module.parse_kokoro_voice("af_nicole:0.7, af_bella:0.3", VOICES)
    assert [n for n, _ in parsed] == ["af_nicole", "af_bella"]
    assert [w for _, w in parsed] == pytest.approx([0.7, 0.3])
    assert [w for _, w in tts_module.parse_kokoro_voice("af_nicole:2,af_bella:6")] == pytest.approx([0.25, 0.75])


@pytest.mark.parametrize("spec", ["", " , ", "af_nicole:", "af_nicole:abc", "af_nicole:0", "af_nicole:-1", ":0.5", "af_nicole:nan", "af_nicole:inf"])
def test_parse_voice_rejects_malformed(spec):
    with pytest.raises(ValueError):
        tts_module.parse_kokoro_voice(spec)


def test_parse_voice_unknown_lists_valid_voices():
    with pytest.raises(ValueError) as e:
        tts_module.parse_kokoro_voice("af_nicole,af_nope", VOICES)
    assert "af_nope" in str(e.value) and "af_bella, af_nicole, bm_george" in str(e.value)


def test_validate_config_only_checks_kokoro(monkeypatch, fake_kokoro):
    monkeypatch.setattr(settings, "TTS_ENGINE", "edge")
    monkeypatch.setattr(settings, "KOKORO_VOICE", "garbage:x")
    tts_module.validate_tts_config()  # not the selected engine
    monkeypatch.setattr(settings, "TTS_ENGINE", "kokoro")
    monkeypatch.setattr(tts_module, "kokoro_available", lambda: False)
    with pytest.raises(ValueError):
        tts_module.validate_tts_config()
    monkeypatch.setattr(settings, "KOKORO_VOICE", "af_nicole")
    tts_module.validate_tts_config()  # files missing is a log line, not an error


def test_validate_config_unknown_voice_is_a_startup_error(monkeypatch, fake_kokoro):
    monkeypatch.setattr(tts_module, "kokoro_available", lambda: True)
    monkeypatch.setattr(tts_module, "kokoro_voice_names", lambda: VOICES)
    monkeypatch.setattr(settings, "TTS_ENGINE", "kokoro")
    monkeypatch.setattr(settings, "KOKORO_VOICE", "af_nope")
    with pytest.raises(ValueError, match="valid voices"):
        tts_module.validate_tts_config()


def test_gain_math():
    x = np.array([0.1, -0.05], dtype=np.float32)
    assert tts_module.apply_gain(x, 0.0) == pytest.approx(x)
    assert tts_module.apply_gain(x, 6.0206) == pytest.approx(x * 2, rel=1e-4)
    assert tts_module.apply_gain(x, -6.0206) == pytest.approx(x / 2, rel=1e-4)
    loud = tts_module.apply_gain(np.array([0.5, -0.4], dtype=np.float32), 20.0)  # would peak at 5.0
    assert float(np.max(np.abs(loud))) == pytest.approx(10 ** (-1 / 20))
    assert loud[1] / loud[0] == pytest.approx(-0.8)  # scaled, not clipped
    assert tts_module.apply_gain(np.array([], dtype=np.float32), 6.0).size == 0


@pytest.mark.anyio
async def test_kokoro_gain_is_applied_to_the_wav(audio_dir, fake_kokoro, monkeypatch):
    monkeypatch.setattr(settings, "KOKORO_GAIN_DB", 6.0206)
    out = await tts_module.generate_audio_clip({"timestamp": 1.0, "text": "hi"}, video_id="job1")
    with wave.open(tts_module.clip_path("job1", out["audio_url"]), "rb") as w:
        sample = np.frombuffer(w.readframes(10), dtype="<i2")[0]
    assert sample == pytest.approx(0.2 * 32767, abs=2)


@pytest.mark.anyio
@pytest.mark.skipif(not tts_module.kokoro_available(), reason="kokoro-onnx or its model files are not installed (see README)")
async def test_kokoro_real_synthesis(audio_dir):
    out = await tts_module.generate_audio_clip({"timestamp": 1.0, "text": "The lion takes a bow."}, video_id="job1")
    assert out is not None and out["audio_url"].endswith(".wav")
    assert 0.5 < out["duration_sec"] < 10
    assert os.path.getsize(tts_module.clip_path("job1", out["audio_url"])) > 10_000
