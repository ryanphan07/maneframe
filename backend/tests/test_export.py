import os
import shutil
import subprocess

import cv2
import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import settings
from app.main import app
from app.schemas.events import CommentaryQuip, CommentaryResponse
from app.services import export as export_module
from app.services.export import (
    ExportError, ExportQuip, build_ass, build_ffmpeg_args, build_filter_graph, caption_spans, duck_intervals,
)
from app.services.jobs import job_store

FFMPEG = "ffmpeg"


def spoken(ts, dur, name="a"):
    return ExportQuip(ts, f"line {name}", f"/audio/{name}.mp3", dur)


def silent(ts, text="no audio here"):
    return ExportQuip(ts, text)


# ---- pure builders ---------------------------------------------------------------------------


def test_caption_spans_use_audio_length_and_three_seconds_without_audio():
    spans = caption_spans([spoken(1.0, 2.5), silent(20.0, "hello")])
    assert spans == [(1.0, 3.5, "line a"), (20.0, 23.0, "hello")]


def test_caption_never_runs_into_the_next_one():
    spans = caption_spans([silent(1.0, "first"), spoken(2.0, 2.0)])
    assert spans[0] == (1.0, 2.0, "first")


def test_ass_has_one_dialogue_per_quip_with_times_text_and_video_size():
    ass = build_ass([spoken(65.5, 2.0), silent(100.0, "x {\\b1} y")], 1080, 1920)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Dialogue: 0,0:01:05.50,0:01:07.50,Default,,0,0,0,,line a" in ass
    assert "0:01:40.00,0:01:43.00,Default,,0,0,0,,x b1 y" in ass  # override braces and backslashes are stripped
    assert ass.count("Dialogue:") == 2


def test_duck_intervals_cover_spoken_quips_only_and_merge_close_ones():
    quips = [spoken(10.0, 2.0, "a"), spoken(12.3, 1.0, "b"), spoken(30.0, 2.0, "c"), silent(50.0)]
    assert duck_intervals(quips) == [(10.0, 13.3), (30.0, 32.0)]


def args_for(quips, has_audio=True):
    return build_ffmpeg_args(FFMPEG, "C:/up/v.mp4", "out.partial.mp4", 60.0, has_audio, quips)


def graph_of(quips, has_audio=True, captions="captions.ass"):
    return build_filter_graph(60.0, has_audio, quips, captions)


def test_args_mix_delayed_quips_over_ducked_original_audio():
    quips = [spoken(1.5, 2.0, "a"), spoken(10.0, 2.0, "b")]
    args = args_for(quips)
    graph = graph_of(quips)
    assert [args[i + 1] for i, a in enumerate(args) if a == "-i"] == ["C:/up/v.mp4", "/audio/a.mp3", "/audio/b.mp3"]
    assert "[0:a]volume=-10dB:enable='between(t,1.5,3.5)+between(t,10,12)'[base]" in graph
    assert "[1:a]adelay=1500:all=1[q1]" in graph and "[2:a]adelay=10000:all=1[q2]" in graph
    assert "[base][q1][q2]amix=inputs=3:duration=first:normalize=0" in graph
    assert args[args.index("-filter_complex_script") + 1] == "filter_graph.txt" and "-filter_complex" not in args
    assert args[args.index("-map") + 1] == "[vout]" and "[aout]" in args
    assert args[args.index("-crf") + 1] == "23"
    assert "libx264" in args and "aac" in args and args[-1] == "out.partial.mp4"


def test_captions_use_a_relative_subtitle_filename():
    graph = graph_of([spoken(1.0, 2.0)])
    assert "subtitles=captions.ass" in graph
    assert ":" not in graph.split("subtitles=")[1].split("[")[0]  # no drive letter or path to escape
    assert "subtitles" not in graph_of([spoken(1.0, 2.0)], captions=None)


def test_source_without_audio_gets_a_silent_base_and_no_ducking():
    graph = graph_of([spoken(1.0, 2.0)], has_audio=False)
    assert "anullsrc" in graph and "volume=" not in graph and "[0:a]" not in graph


def test_no_spoken_quips_keeps_the_original_audio_untouched():
    args = args_for([silent(1.0)])
    assert "amix" not in graph_of([silent(1.0)])
    assert args[args.index("0:a:0") - 1] == "-map"


def test_no_spoken_quips_and_no_source_audio_means_no_audio_stream():
    args = args_for([silent(1.0)], has_audio=False)
    assert "-c:a" not in args and "[aout]" not in args


def test_output_dimensions_are_kept_even_and_duration_capped():
    args = args_for([spoken(1.0, 2.0)])
    assert "scale=trunc(iw/2)*2:trunc(ih/2)*2" in graph_of([spoken(1.0, 2.0)])
    assert args[args.index("-t") + 1] == "60"


# ---- endpoint --------------------------------------------------------------------------------


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(upload_dir):
    with TestClient(app) as c:
        yield c


def completed_job(upload_dir, job_id="a" * 32, name="clip.avi"):
    video = upload_dir / "videos" / f"{job_id}_{name}"
    video.parent.mkdir(exist_ok=True)
    video.write_bytes(b"video")
    job_store.create(str(video), job_id=job_id)
    job_store.complete(job_id, CommentaryResponse(quips=[CommentaryQuip(timestamp=1.0, text="hi")]))
    return job_id


def test_export_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope/export.mp4").status_code == 404


def test_export_before_completion_is_409(client, upload_dir):
    job_store.create(str(upload_dir / "v.avi"), job_id="running")
    resp = client.get("/api/jobs/running/export.mp4")
    assert resp.status_code == 409 and "not ready" in resp.json()["detail"]


def test_export_returns_the_rendered_file_named_after_the_original(client, upload_dir, monkeypatch):
    job_id = completed_job(upload_dir, name="Lion_dance_1.mov")
    seen = {}

    async def fake_render(jid, video_path, quips, captions):
        seen.update(jid=jid, video_path=video_path, quips=quips, captions=captions)
        out = upload_dir / "out.mp4"
        out.write_bytes(b"mp4 bytes")
        return str(out)

    monkeypatch.setattr(main_module, "render_export", fake_render)
    resp = client.get(f"/api/jobs/{job_id}/export.mp4?captions=0")
    assert resp.status_code == 200 and resp.content == b"mp4 bytes"
    assert resp.headers["content-type"] == "video/mp4"
    assert "maneframe_Lion_dance_1.mp4" in resp.headers["content-disposition"]
    assert seen["captions"] is False and seen["quips"][0]["text"] == "hi"


def test_post_export_renders_and_returns_the_download_url(client, upload_dir, monkeypatch):
    job_id = completed_job(upload_dir)
    seen = []

    async def fake_render(jid, video_path, quips, captions):
        seen.append(captions)
        return str(upload_dir / "out.mp4")

    monkeypatch.setattr(main_module, "render_export", fake_render)
    resp = client.post(f"/api/jobs/{job_id}/export?captions=0")
    assert resp.status_code == 200 and resp.json() == {"url": f"/api/jobs/{job_id}/export.mp4?captions=0"}
    assert seen == [False]
    assert client.post("/api/jobs/nope/export").status_code == 404


def test_post_export_failure_is_a_json_error(client, upload_dir, monkeypatch):
    job_id = completed_job(upload_dir)

    async def failing(*a, **k):
        raise ExportError("Rendering the video failed.")

    monkeypatch.setattr(main_module, "render_export", failing)
    resp = client.post(f"/api/jobs/{job_id}/export")
    assert resp.status_code == 500 and resp.json() == {"detail": "Rendering the video failed."}


def test_export_failure_is_a_clear_error_never_a_file(client, upload_dir, monkeypatch):
    job_id = completed_job(upload_dir)

    async def failing(*a, **k):
        raise ExportError("Rendering the video failed.")

    monkeypatch.setattr(main_module, "render_export", failing)
    resp = client.get(f"/api/jobs/{job_id}/export.mp4")
    assert resp.status_code == 500 and resp.json() == {"detail": "Rendering the video failed."}


def test_export_with_the_source_video_gone_is_404(client, upload_dir):
    job_id = completed_job(upload_dir)
    os.remove(upload_dir / "videos" / f"{job_id}_clip.avi")
    resp = client.get(f"/api/jobs/{job_id}/export.mp4")
    assert resp.status_code == 404 and "no longer available" in resp.json()["detail"]


def test_export_with_missing_audio_file_is_an_error(upload_dir):
    quips = [{"timestamp": 1.0, "text": "x", "audio_url": "http://h/uploads/audio/job1/audio_1.00_ab.mp3", "duration_sec": 1.0}]
    with pytest.raises(ExportError, match="audio is no longer available"):
        export_module.export_quips("job1", quips)


def test_ffmpeg_path_setting_must_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "FFMPEG_PATH", str(tmp_path / "nope.exe"))
    with pytest.raises(ExportError):
        export_module.resolve_ffmpeg()
    assert not export_module.ffmpeg_available()


# ---- integration: a tiny real video through real ffmpeg --------------------------------------


@pytest.mark.skipif(not export_module.ffmpeg_available(), reason="no ffmpeg binary available")
@pytest.mark.anyio
async def test_real_render_has_h264_aac_original_size_and_burned_captions(upload_dir):
    ffmpeg = export_module.resolve_ffmpeg()
    job_id = "b" * 32
    video = upload_dir / "videos" / f"{job_id}_tiny.mp4"
    video.parent.mkdir()
    audio_dir = upload_dir / "audio" / job_id
    audio_dir.mkdir(parents=True)

    def run(*a):
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *a], check=True)

    run("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=6", "-f", "lavfi", "-i", "sine=frequency=220:duration=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video))
    run("-f", "lavfi", "-i", "sine=frequency=880:duration=1.5", str(audio_dir / "audio_1.00_ab.mp3"))

    quips = [
        {"timestamp": 1.0, "text": "A big opening line", "audio_url": "http://h/uploads/audio/x/audio_1.00_ab.mp3", "duration_sec": 1.5},
        {"timestamp": 3.5, "text": "Silent caption only", "audio_url": None, "duration_sec": None},
    ]
    out = await export_module.render_export(job_id, str(video), quips, captions=True)
    assert out == export_module.export_file(job_id, True) and os.path.getsize(out) > 0
    assert not list((upload_dir / "exports" / job_id).glob("*.partial.mp4"))

    cap = cv2.VideoCapture(out)
    assert (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))) == (320, 240)
    assert cap.get(cv2.CAP_PROP_FRAME_COUNT) == pytest.approx(60, abs=3)
    cap.release()
    probe = subprocess.run([ffmpeg, "-hide_banner", "-i", out], capture_output=True, text=True).stderr
    assert "Video: h264" in probe and "Audio: aac" in probe

    # captions are really burned in: the bottom of a frame during the caption differs from the caption-free render
    plain = await export_module.render_export(job_id, str(video), quips, captions=False)
    assert plain != out

    def bottom_strip(path, second):
        c = cv2.VideoCapture(path)
        c.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
        ok, frame = c.read()
        c.release()
        assert ok
        return frame[int(frame.shape[0] * 0.8):].astype(int)

    assert abs(bottom_strip(out, 1.5) - bottom_strip(plain, 1.5)).mean() > 5

    assert await export_module.render_export(job_id, str(video), quips, captions=True) == out  # cached


@pytest.mark.skipif(not export_module.ffmpeg_available(), reason="no ffmpeg binary available")
@pytest.mark.anyio
async def test_real_render_with_300_quips_stays_under_the_command_line_limit(upload_dir):
    ffmpeg = export_module.resolve_ffmpeg()
    job_id = "c" * 32
    video = upload_dir / "videos" / f"{job_id}_many.mp4"
    video.parent.mkdir()
    audio_dir = upload_dir / "audio" / job_id
    audio_dir.mkdir(parents=True)

    def run(*a):
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *a], check=True)

    run("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=5:duration=65", "-f", "lavfi", "-i", "sine=frequency=220:duration=65",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video))
    run("-f", "lavfi", "-i", "sine=frequency=880:duration=0.2", str(audio_dir / "seed.mp3"))
    quips = []
    for i in range(300):
        name = f"audio_{i * 0.2:.2f}_{i:04d}.mp3"
        shutil.copy(audio_dir / "seed.mp3", audio_dir / name)
        quips.append({"timestamp": i * 0.2, "text": f"quip {i}", "audio_url": f"http://h/uploads/audio/x/{name}", "duration_sec": 0.2})

    seen = {}
    real_args = export_module.build_ffmpeg_args

    def spy(*a, **k):
        seen["args"] = real_args(*a, **k)
        return seen["args"]

    export_module.build_ffmpeg_args = spy
    try:
        out = await export_module.render_export(job_id, str(video), quips, captions=True)
    finally:
        export_module.build_ffmpeg_args = real_args
    assert os.path.getsize(out) > 0
    assert sum(len(a) + 1 for a in seen["args"]) < 32000  # the Windows CreateProcess limit is 32767
    assert len(seen["args"]) > 600  # one -i per quip, none of the graph
    assert (upload_dir / "exports" / job_id / "filter_graph.txt").read_text().count("adelay") == 300
