import os
import wave

import pytest

from app.config import settings
from app.services import tts as tts_module


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "TTS_ENABLED", True)
    monkeypatch.setattr(settings, "TTS_ENGINE", "piper")
    return tmp_path / "audio" / "job1"


def files_in(d):
    return os.listdir(d) if d.exists() else []


class FakeConfig:
    def __init__(self, length_scale=1.0):
        self.length_scale = length_scale


class FakeVoice:
    behavior = "ok"
    loads = []
    spoken = []

    @classmethod
    def load(cls, path):
        cls.loads.append(path)
        return cls()

    def synthesize_wav(self, text, wav_file, config):
        FakeVoice.spoken.append((text, config.length_scale))
        if FakeVoice.behavior == "boom":
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x01\x00" * 100)  # partial output before the failure
            raise RuntimeError("onnx failed")
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x01\x00" * (22050 * 2 if FakeVoice.behavior == "ok" else 0))  # 2 s, or none


@pytest.fixture
def fake_piper(tmp_path, monkeypatch):
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"x")
    FakeVoice.behavior = "ok"
    FakeVoice.loads = []
    FakeVoice.spoken = []
    monkeypatch.setattr(settings, "PIPER_MODEL_PATH", str(model))
    monkeypatch.setattr(tts_module, "PiperVoice", FakeVoice)
    monkeypatch.setattr(tts_module, "SynthesisConfig", FakeConfig)
    monkeypatch.setattr(tts_module, "_piper_voice", None)
    monkeypatch.setattr(tts_module, "_piper_voice_path", None)
    return FakeVoice


@pytest.mark.anyio
async def test_piper_returns_wav_url_and_measured_duration(audio_dir, fake_piper):
    out = await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1")
    assert out["audio_url"].endswith(".wav") and "/uploads/audio/job1/" in out["audio_url"]
    assert out["duration_sec"] == pytest.approx(2.0)
    assert files_in(audio_dir) == [os.path.basename(out["audio_url"])]
    assert fake_piper.spoken == [("hello", settings.PIPER_LENGTH_SCALE)]


@pytest.mark.anyio
async def test_piper_loads_the_voice_once(audio_dir, fake_piper):
    for t in (1.0, 6.0):
        assert await tts_module.generate_audio_clip({"timestamp": t, "text": f"line {t}"}, video_id="job1")
    assert len(fake_piper.loads) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("behavior", ["boom", "empty"])
async def test_piper_failure_gives_no_audio_and_no_file(audio_dir, fake_piper, behavior):
    fake_piper.behavior = behavior
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    assert files_in(audio_dir) == []


@pytest.mark.anyio
async def test_piper_missing_model_gives_no_audio(audio_dir, fake_piper, monkeypatch):
    monkeypatch.setattr(settings, "PIPER_MODEL_PATH", str(audio_dir / "nope.onnx"))
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None
    assert files_in(audio_dir) == []


@pytest.mark.anyio
async def test_piper_not_installed_gives_no_audio(audio_dir, fake_piper, monkeypatch):
    monkeypatch.setattr(tts_module, "PiperVoice", None)
    assert await tts_module.generate_audio_clip({"timestamp": 2.0, "text": "hello"}, video_id="job1") is None


def test_clip_path_accepts_wav_and_mp3_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    assert tts_module.clip_path("job1", "/uploads/audio/job1/a_1.00_ab.wav").endswith("a_1.00_ab.wav")
    assert tts_module.clip_path("job1", "/uploads/audio/job1/a_1.00_ab.mp3").endswith("a_1.00_ab.mp3")
    assert tts_module.clip_path("job1", "/uploads/audio/job1/a.ogg") is None


def test_measure_duration_reads_wav(tmp_path):
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 8000)
    assert tts_module.measure_duration(str(path)) == pytest.approx(0.5)
    path.write_bytes(b"not a wav")
    assert tts_module.measure_duration(str(path)) is None


def test_edge_stays_the_default():
    assert type(settings).model_fields["TTS_ENGINE"].default == "edge"


@pytest.mark.anyio
@pytest.mark.skipif(not tts_module.piper_available(), reason="piper-tts or the voice model is not installed (see README)")
async def test_piper_real_synthesis(audio_dir):
    out = await tts_module.generate_audio_clip({"timestamp": 1.0, "text": "The lion takes a bow."}, video_id="job1")
    assert out is not None and out["audio_url"].endswith(".wav")
    assert 0.5 < out["duration_sec"] < 10
    path = tts_module.clip_path("job1", out["audio_url"])
    assert os.path.getsize(path) > 10_000
