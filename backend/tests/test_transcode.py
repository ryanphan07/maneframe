import subprocess

import cv2
import pytest

from app.services import export as export_module
from app.services import transcode
from app.services import video_processor as vp

pytestmark = pytest.mark.skipif(not export_module.ffmpeg_available(), reason="no ffmpeg binary available")

W, H = 160, 96


def ffmpeg(*args):
    subprocess.run([export_module.resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def probe(path):
    return subprocess.run([export_module.resolve_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr


def make_clip(path, codec="libx264", rotation=0):
    """A landscape W x H test clip with an audio track; `rotation` is stored as display-matrix metadata only."""
    raw = path.with_suffix(".raw.mp4") if rotation else path
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=10:duration=4", "-f", "lavfi", "-i", "sine=frequency=220:duration=4",
           "-c:v", codec, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(raw))
    if rotation:
        ffmpeg("-display_rotation", str(rotation), "-i", str(raw), "-c", "copy", str(path))
    return str(path)


def frame_shape(path):
    cap = cv2.VideoCapture(path)
    ok, frame = cap.read()
    cap.release()
    assert ok
    return frame.shape[:2]


def test_parse_probe_reads_codec_and_rotation():
    text = "  Stream #0:0: Video: hevc (Main), yuv420p, 1920x1080\n      Side data:\n        displaymatrix: rotation of -90.00 degrees\n"
    assert transcode.parse_probe(text) == ("hevc", 270)
    assert transcode.parse_probe("  Stream #0:0(und): Video: h264 (High), yuv420p") == ("h264", 0)
    assert transcode.parse_probe("garbage") == (None, 0)


def test_needs_transcode_rules():
    assert not transcode.needs_transcode("h264", 0, True)
    assert transcode.needs_transcode("h264", 90, True)
    assert transcode.needs_transcode("hevc", 0, True)
    assert transcode.needs_transcode("h264", 0, False)


@pytest.mark.anyio
async def test_plain_h264_clip_is_left_alone(tmp_path):
    clip = make_clip(tmp_path / "plain.mp4")
    before = open(clip, "rb").read()
    assert await transcode.normalize_video(clip, True) is False
    assert open(clip, "rb").read() == before


@pytest.mark.anyio
async def test_rotated_phone_clip_comes_out_upright(tmp_path):
    clip = make_clip(tmp_path / "phone.mp4", rotation=90)
    assert "rotation of" in probe(clip)
    assert await transcode.normalize_video(clip, True) is True
    assert "rotation of" not in probe(clip) and "Video: h264" in probe(clip) and "Audio: aac" in probe(clip)
    assert frame_shape(clip) == (W, H)  # portrait: height W, width H
    assert vp.get_video_duration(clip) == pytest.approx(4.0, abs=0.5)
    frames = vp.sample_window_frames(clip, 0.0, 4.0, 3, 64, None)
    assert frames
    assert not list(tmp_path.glob("*.transcode.mp4"))


@pytest.mark.skipif("libx265" not in subprocess.run([export_module.resolve_ffmpeg() if export_module.ffmpeg_available() else "ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout,
                    reason="this ffmpeg build has no HEVC encoder")
@pytest.mark.anyio
async def test_hevc_clip_becomes_h264(tmp_path):
    clip = make_clip(tmp_path / "hevc.mp4", codec="libx265")
    assert "Video: hevc" in probe(clip)
    assert await transcode.normalize_video(clip, True) is True
    assert "Video: h264" in probe(clip)
    assert not vp.is_video_corrupt(clip)


@pytest.mark.anyio
async def test_unreadable_by_opencv_forces_a_transcode(tmp_path):
    clip = make_clip(tmp_path / "x.mp4")
    assert await transcode.normalize_video(clip, False) is True


@pytest.mark.anyio
async def test_not_a_video_is_left_for_the_corruption_check(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    assert await transcode.normalize_video(str(bad), False) is False
    assert bad.read_bytes() == b"not a video"


@pytest.mark.anyio
async def test_failed_transcode_raises_and_keeps_no_partial(tmp_path, monkeypatch):
    clip = make_clip(tmp_path / "x.mp4")
    real_run = export_module._run

    async def failing(args, cwd, timeout):
        if "libx264" in args:
            return 1, "boom"
        return await real_run(args, cwd, timeout)

    monkeypatch.setattr(export_module, "_run", failing)
    with pytest.raises(export_module.ExportError):
        await transcode.normalize_video(clip, False)
    assert not list(tmp_path.glob("*.transcode.mp4"))


def test_job_stores_a_rotated_phone_clip_upright(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_module
    from app.config import settings
    from app.main import app
    from app.services import llm as llm_module
    from app.services import tts as tts_module

    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(main_module, "VIDEO_DIR", str(tmp_path / "videos"))
    (tmp_path / "videos").mkdir()

    async def fake_llm(frames, start, end, previous=None, label="", **_):
        return llm_module.WindowResult([{"timestamp": 0.0, "text": "hi"}])

    async def fake_tts(quip, video_id=None):
        return {"timestamp": quip["timestamp"], "text": quip["text"], "audio_url": None}

    monkeypatch.setattr(llm_module, "analyze_window", fake_llm)
    monkeypatch.setattr(tts_module, "generate_audio_clip", fake_tts)
    clip = make_clip(tmp_path / "phone.mp4", rotation=90)
    with TestClient(app) as client:
        resp = client.post("/api/upload", files={"file": ("phone.mp4", open(clip, "rb").read(), "video/mp4")})
        assert resp.status_code == 200, resp.text
        assert client.get(f"/api/jobs/{resp.json()['job_id']}").json()["status"] == "completed"
    stored = next((tmp_path / "videos").iterdir())
    assert "rotation of" not in probe(stored) and frame_shape(str(stored)) == (W, H)
