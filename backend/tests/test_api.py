import mimetypes
import os

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main as main_module
from app.config import settings
from app.main import app
from app.schemas.events import CommentaryQuip, CommentaryResponse
from app.services import llm as llm_module
from app.services import pipeline as pipeline_module
from app.services import tts as tts_module
from app.services.jobs import job_store


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(main_module, "VIDEO_DIR", str(tmp_path / "videos"))
    os.makedirs(tmp_path / "videos")
    return tmp_path


@pytest.fixture
def client(upload_dir):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_ai(monkeypatch):
    monkeypatch.setattr(settings, "MIN_QUIP_GAP_SEC", 2.5)
    async def fake_llm(frames, start, end, previous=None, label="", **_):
        return llm_module.WindowResult([{"timestamp": 0.0, "text": "first"}, {"timestamp": 3.0, "text": "second"}])

    async def fake_tts(quip, video_id=None):
        return {"timestamp": quip["timestamp"], "text": quip["text"], "audio_url": f"http://test/{video_id}/{quip['timestamp']}.mp3"}

    monkeypatch.setattr(llm_module, "analyze_window", fake_llm)
    monkeypatch.setattr(tts_module, "generate_audio_clip", fake_tts)


@pytest.fixture
def video_bytes(tmp_path):
    path = str(tmp_path / "src.avi")
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 5.0, (64, 64))
    for i in range(15):
        writer.write(np.full((64, 64, 3), i * 10, dtype=np.uint8))
    writer.release()
    with open(path, "rb") as f:
        return f.read()


def upload(client, data, name="clip.avi"):
    return client.post("/api/upload", files={"file": (name, data, "video/x-msvideo")})


def test_upload_runs_pipeline_and_completes(client, upload_dir, fake_ai, video_bytes):
    resp = upload(client, video_bytes, "../my clip!.avi")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"job_id", "filename"}
    assert body["filename"] == "my_clip_.avi"
    assert str(upload_dir) not in resp.text

    job = client.get(f"/api/jobs/{body['job_id']}").json()
    assert job["status"] == "completed"
    assert job["progress"] == 100
    assert [q["text"] for q in job["result"]["quips"]] == ["first", "second"]
    assert job["result"]["quips"][0]["audio_url"].startswith(f"http://test/{body['job_id']}/")
    assert os.path.exists(upload_dir / "videos" / f"{body['job_id']}_my_clip_.avi")  # kept for mp4 export until the job expires
    assert not (upload_dir / "frames").exists()


def test_upload_rejects_bad_extension_without_writing(client, upload_dir, video_bytes):
    resp = upload(client, video_bytes, "evil.exe")
    assert resp.status_code == 400
    assert os.listdir(upload_dir / "videos") == []


def test_upload_rejects_missing_filename(client, upload_dir, video_bytes):
    resp = client.post("/api/upload", files={"file": ("", video_bytes, "video/x-msvideo")})
    assert resp.status_code in (400, 422)
    assert os.listdir(upload_dir / "videos") == []


def failed_job(client, resp):
    assert resp.status_code == 200
    job = client.get(f"/api/jobs/{resp.json()['job_id']}").json()
    assert job["status"] == "error"
    return job


def test_corrupt_video_fails_the_job_and_removes_file(client, upload_dir):
    job = failed_job(client, upload(client, b"not a video"))
    assert job["message"] == "The uploaded video file is corrupt."
    assert os.listdir(upload_dir / "videos") == []


def test_pipeline_failure_marks_error_and_cleans_up(client, upload_dir, video_bytes, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(llm_module, "analyze_window", boom)
    job_id = upload(client, video_bytes).json()["job_id"]

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "error"
    assert job["message"] == "Commentary generation failed"
    assert "secret" not in str(job)
    assert os.listdir(upload_dir / "videos") == []
    assert not (upload_dir / "audio" / job_id).exists()


@pytest.mark.anyio
async def test_run_pipeline_survives_failing_progress_update(upload_dir, fake_ai, video_bytes, monkeypatch):
    path = str(upload_dir / "v.avi")
    with open(path, "wb") as f:
        f.write(video_bytes)
    job_store.create(path, job_id="cbfail")
    real_update = job_store.update

    def bad_update(job_id, status, progress, message=None):
        if status != "completed":
            raise RuntimeError("update broke")
        return real_update(job_id, status, progress, message)

    monkeypatch.setattr(job_store, "update", bad_update)
    await pipeline_module.run_pipeline("cbfail", path)
    assert job_store.get("cbfail").status == "completed"


def test_get_unknown_job_404(client):
    resp = client.get("/api/jobs/nope")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Job not found"}


def test_ws_replays_completed_job_and_closes(client):
    job_store.create("x.mp4", job_id="done1")
    job_store.complete("done1", CommentaryResponse(quips=[CommentaryQuip(timestamp=0, text="hi")]))
    with client.websocket_connect("/ws/analyze/done1") as ws:
        msg = ws.receive_json()
        assert msg["status"] == "completed"
        assert msg["progress"] == 100
        assert "result" not in msg and "quips" not in msg
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1000


def test_ws_unknown_job_4404(client):
    with client.websocket_connect("/ws/analyze/missing") as ws:
        assert ws.receive_json() == {"status": "error", "progress": 0, "message": "Job not found"}
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4404


def test_ws_streams_live_progress_and_unsubscribes(client):
    job_store.create("x.mp4", job_id="live1")
    with client.websocket_connect("/ws/analyze/live1") as ws:
        assert ws.receive_json()["status"] == "uploading"
        client.portal.call(job_store.update, "live1", "extracting", 10)
        assert ws.receive_json() == {"status": "extracting", "progress": 10}
        client.portal.call(job_store.update, "live1", "analyzing", 40, "thinking")
        assert ws.receive_json() == {"status": "analyzing", "progress": 40, "message": "thinking"}
        client.portal.call(job_store.fail, "live1", "Commentary generation failed")
        msg = ws.receive_json()
        assert msg["status"] == "error" and msg["message"] == "Commentary generation failed"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1000
    assert job_store._subscribers["live1"] == []


def test_ws_client_disconnect_while_idle_unsubscribes(client):
    job_store.create("x.mp4", job_id="idle1")
    with client.websocket_connect("/ws/analyze/idle1") as ws:
        assert ws.receive_json()["status"] == "uploading"
    for _ in range(50):
        if job_store._subscribers.get("idle1") == []:
            break
        client.portal.call(lambda: None)
    assert job_store._subscribers["idle1"] == []


def test_videos_are_not_served(client, upload_dir, fake_ai, video_bytes):
    job_id = upload(client, video_bytes).json()["job_id"]
    assert client.get(f"/uploads/videos/{job_id}_clip.avi").status_code == 404


def test_video_file_on_disk_is_not_served(client):
    real_video_dir = os.path.join(os.path.dirname(main_module.__file__), "..", "uploads", "videos")
    os.makedirs(real_video_dir, exist_ok=True)
    probe = os.path.join(real_video_dir, "probe_serving_test.avi")
    with open(probe, "wb") as f:
        f.write(b"x")
    try:
        assert client.get("/uploads/videos/probe_serving_test.avi").status_code == 404
        assert client.get("/uploads/probe_serving_test.avi").status_code == 404
    finally:
        os.remove(probe)


def test_too_long_video_fails_the_job_and_removes_file(client, upload_dir, video_bytes, monkeypatch):
    monkeypatch.setattr(settings, "MAX_VIDEO_SECONDS", 1.0)
    job = failed_job(client, upload(client, video_bytes))  # 3s clip
    assert "too long" in job["message"]
    assert os.listdir(upload_dir / "videos") == []


def test_zero_fps_video_fails_the_job_and_removes_file(client, upload_dir, video_bytes, monkeypatch):
    monkeypatch.setattr(pipeline_module, "get_video_duration", lambda p: (_ for _ in ()).throw(ValueError("The uploaded video could not be read.")))
    job = failed_job(client, upload(client, video_bytes))
    assert job["message"] == "The uploaded video could not be read."
    assert os.listdir(upload_dir / "videos") == []


def test_get_video_duration_and_invalid_fps(video_bytes, tmp_path, monkeypatch):
    from app.services import video_processor as vp

    path = str(tmp_path / "d.avi")
    with open(path, "wb") as f:
        f.write(video_bytes)
    assert vp.get_video_duration(path) == pytest.approx(3.0, abs=0.3)

    class FakeCap:
        released = False
        def isOpened(self): return True
        def get(self, prop): return 0
        def release(self): FakeCap.released = True

    monkeypatch.setattr(vp.cv2, "VideoCapture", lambda p: FakeCap())
    with pytest.raises(ValueError):
        vp.get_video_duration(path)
    assert FakeCap.released


def test_unhandled_error_returns_generic_500(upload_dir, monkeypatch):
    def boom(_):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(main_module.job_store, "get", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/jobs/anything")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}


def test_save_failure_cleans_up_partial_file(client, upload_dir, video_bytes, monkeypatch):
    monkeypatch.setattr(main_module.job_store, "create", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/api/upload", files={"file": ("clip.avi", video_bytes, "video/x-msvideo")}
    )
    assert resp.status_code == 500
    assert os.listdir(upload_dir / "videos") == []


@pytest.mark.anyio
async def test_cleanup_loop_purges_files_survives_errors_and_cancels(upload_dir, monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "CLEANUP_INTERVAL_SECONDS", 0.01)
    video = upload_dir / "videos" / "old_clip.avi"
    video.write_bytes(b"x")
    (upload_dir / "audio" / "oldjob").mkdir(parents=True)
    job_store.create(str(video), job_id="oldjob")
    job_store.fail("oldjob", "x")
    job_store.get("oldjob").updated_at = 0

    calls = {"n": 0}
    real_purge = job_store.purge_expired

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_purge()

    monkeypatch.setattr(job_store, "purge_expired", flaky)
    task = asyncio.create_task(main_module._cleanup_loop())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if not video.exists():
            break
    assert not video.exists()
    assert not (upload_dir / "audio" / "oldjob").exists()
    assert calls["n"] >= 2  # survived the first exception
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_validate_upload_size_limit_is_a_setting(monkeypatch):
    from app.services import video_processor as vp
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)
    vp.validate_upload_metadata("a.mp4", 1024 * 1024)
    with pytest.raises(ValueError, match="1MB"):
        vp.validate_upload_metadata("a.mp4", 1024 * 1024 + 1)


def test_startup_sweep_removes_only_app_created_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    job = "a" * 32
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / f"{job}_old.mp4").write_bytes(b"x")
    (tmp_path / "videos" / "holiday.mp4").write_bytes(b"mine")
    (tmp_path / "videos" / ("b" * 32)).mkdir()  # a directory, not a video
    (tmp_path / "audio" / job).mkdir(parents=True)
    (tmp_path / "audio" / job / "a.mp3").write_bytes(b"fake-audio")
    (tmp_path / "audio" / "my-music").mkdir()
    (tmp_path / "audio" / "my-music" / "song.mp3").write_bytes(b"mine")
    (tmp_path / "audio" / "notes.txt").write_text("mine")
    from app.services.pipeline import sweep_uploads
    assert sweep_uploads() == 2
    assert sorted(os.listdir(tmp_path / "videos")) == ["b" * 32, "holiday.mp4"]
    assert sorted(os.listdir(tmp_path / "audio")) == ["my-music", "notes.txt"]
    assert (tmp_path / "audio" / "my-music" / "song.mp3").exists()


def test_second_upload_while_a_job_runs_is_rejected(client, upload_dir, video_bytes, fake_ai):
    from app.services.jobs import job_store
    job_store.create(str(upload_dir / "videos" / "busy.avi"), job_id="busy")
    try:
        resp = upload(client, video_bytes)
        assert resp.status_code == 409
        assert "still being processed" in resp.json()["detail"]
        assert os.listdir(upload_dir / "videos") == []
        job_store.fail("busy", "done")
        assert upload(client, video_bytes).status_code in (200, 400)
    finally:
        job_store._jobs.pop("busy", None)


def test_friendly_error_categories():
    import httpx
    import openai
    from app.services.llm import LLMOutputError
    from app.services.pipeline import GENERIC_FAILURE, friendly_error

    req = httpx.Request("POST", "http://x")
    assert "not reachable" in friendly_error(openai.APIConnectionError(request=req))
    assert "not reachable" in friendly_error(openai.APITimeoutError(request=req))
    resp = httpx.Response(500, request=req)
    rejected = friendly_error(openai.InternalServerError("boom 500", response=resp, body=None))
    assert rejected.startswith("The AI model rejected the request") and "500" not in rejected
    assert "unusable output" in friendly_error(LLMOutputError("bad json"))
    assert "video" in friendly_error(ValueError("No frames"))
    assert friendly_error(RuntimeError("secret")) == GENERIC_FAILURE


def test_kept_video_is_deleted_when_the_job_expires(client, upload_dir, fake_ai, video_bytes):
    job_id = upload(client, video_bytes).json()["job_id"]
    video = upload_dir / "videos" / f"{job_id}_clip.avi"
    (upload_dir / "exports" / job_id).mkdir(parents=True)
    assert video.exists()

    job_store.get(job_id).updated_at = 0
    assert upload(client, video_bytes).status_code == 200  # the next upload purges the expired job and its files
    assert not video.exists()
    assert not (upload_dir / "exports" / job_id).exists()


def test_upload_style_defaults_to_hype_and_is_in_result(client, upload_dir, fake_ai, video_bytes):
    job_id = upload(client, video_bytes).json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}").json()["result"]["style"] == "hype"


def test_upload_style_is_passed_through_to_the_result(client, upload_dir, fake_ai, video_bytes):
    resp = client.post("/api/upload", files={"file": ("clip.avi", video_bytes, "video/x-msvideo")}, data={"style": "deadpan"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}").json()["result"]["style"] == "deadpan"
    assert job_store.get(job_id).style == "deadpan"


def test_upload_rejects_unknown_style_without_writing(client, upload_dir, video_bytes):
    resp = client.post("/api/upload", files={"file": ("clip.avi", video_bytes, "video/x-msvideo")}, data={"style": "../hype"})
    assert resp.status_code == 400
    assert "broadcast, deadpan, hype" in resp.json()["detail"]
    assert os.listdir(upload_dir / "videos") == []


def test_upload_route_only_stores_the_file(client, upload_dir, video_bytes, monkeypatch):
    """Conversion is the job's first step, not part of the request."""
    async def forbidden(*a, **k):
        raise AssertionError("the upload route must not convert the video")

    async def no_pipeline(*a, **k):
        return None

    monkeypatch.setattr(pipeline_module, "normalize_video", forbidden)
    monkeypatch.setattr(main_module, "run_pipeline", no_pipeline)
    resp = upload(client, video_bytes)
    assert resp.status_code == 200
    stored = upload_dir / "videos" / f"{resp.json()['job_id']}_clip.avi"
    assert stored.read_bytes() == video_bytes
    assert client.get(f"/api/jobs/{resp.json()['job_id']}").json()["status"] == "uploading"


@pytest.mark.anyio
async def test_conversion_is_the_first_job_step_and_reports_progress(upload_dir, video_bytes):
    path = str(upload_dir / "v.avi")
    with open(path, "wb") as f:
        f.write(video_bytes)
    steps = []

    async def cb(status, progress, message=None):
        steps.append((status, message))

    prepared = await pipeline_module.prepare_video(path, cb)
    assert steps == [("extracting", "Converting video")]
    assert prepared.duration == pytest.approx(3.0, abs=0.3)
    assert prepared.media_type == mimetypes.guess_type(path)[0]


def test_job_video_is_served_with_range_support_from_the_job_only(client, upload_dir, fake_ai, video_bytes):
    job_id = upload(client, video_bytes).json()["job_id"]
    full = client.get(f"/api/jobs/{job_id}/video")
    assert full.status_code == 200
    assert full.content == video_bytes and full.headers["content-type"] == mimetypes.guess_type("clip.avi")[0]
    assert full.headers["accept-ranges"] == "bytes"
    part = client.get(f"/api/jobs/{job_id}/video", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == video_bytes[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(video_bytes)}"
    assert str(upload_dir) not in str(part.headers)
    assert client.get(f"/api/jobs/{job_id}/video", headers={"Range": "bytes=999999999-"}).status_code == 416


def test_job_video_errors(client, upload_dir, fake_ai, video_bytes):
    assert client.get("/api/jobs/nope/video").status_code == 404
    job_store.create("x.avi", job_id="early")  # not prepared yet
    assert client.get("/api/jobs/early/video").status_code == 409
    del job_store._jobs["early"]  # an unfinished job would block the next upload
    job_id = upload(client, video_bytes).json()["job_id"]
    os.remove(upload_dir / "videos" / f"{job_id}_clip.avi")
    assert client.get(f"/api/jobs/{job_id}/video").status_code == 404
    assert client.get("/uploads/videos/anything.avi").status_code == 404  # raw uploads stay unreachable


def test_styles_reports_the_upload_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 7)
    assert client.get("/api/styles").json()["max_upload_mb"] == 7


def test_upload_rejected_early_from_content_length(client, monkeypatch, upload_dir):
    """413 comes from the header alone: the route never runs, so nothing is saved and no job exists."""
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)
    ran = []
    monkeypatch.setattr(main_module, "run_pipeline", lambda *a, **k: ran.append(1))
    resp = client.post("/api/upload", content=b"x" * 10, headers={"content-length": str(5 * 1024 * 1024), "content-type": "multipart/form-data; boundary=b"})
    assert resp.status_code == 413
    assert "1MB" in resp.json()["detail"]
    assert os.listdir(upload_dir / "videos") == [] and not ran and not job_store._jobs


def test_upload_just_over_the_limit_still_reaches_the_backstop(client, monkeypatch, video_bytes):
    """Multipart framing makes the body a little larger than the file; only the post-parse check judges the file itself."""
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)
    big = video_bytes + bytes(1) * (1024 * 1024)
    resp = upload(client, big)
    assert resp.status_code == 400
    assert "1MB" in resp.json()["detail"]
