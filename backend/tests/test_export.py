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
    assert "[0:a]volume=-14dB:enable='between(t,1.5,3.5)+between(t,10,12)'[base]" in graph
    assert "[1:a]acompressor=threshold=-24dB:ratio=3:attack=5:release=80,loudnorm=I=-14:TP=-1.5:LRA=7,aresample=48000,adelay=1500:all=1[q1]" in graph
    assert "[2:a]acompressor=threshold=-24dB:ratio=3:attack=5:release=80,loudnorm=I=-14:TP=-1.5:LRA=7,aresample=48000,adelay=10000:all=1[q2]" in graph
    assert "[base][q1][q2]amix=inputs=3:duration=first:normalize=0" in graph
    assert args[args.index("-filter_complex_script") + 1] == "filter_graph.txt" and "-filter_complex" not in args
    assert args[args.index("-map") + 1] == "[vout]" and "[aout]" in args
    assert args[args.index("-crf") + 1] == "23"
    assert "libx264" in args and "aac" in args and args[-1] == "out.partial.mp4"


def test_duck_depth_and_quip_loudness_come_from_arguments_then_settings(monkeypatch):
    quips = [spoken(1.5, 2.0)]
    graph = build_filter_graph(60.0, True, quips, None, duck_db=-20, quip_lufs=-14.5)
    assert "volume=-20dB:enable" in graph and "loudnorm=I=-14.5:" in graph
    monkeypatch.setattr(settings, "EXPORT_DUCK_DB", -6.0)
    monkeypatch.setattr(settings, "EXPORT_QUIP_LUFS", -18.0)
    graph = build_filter_graph(60.0, True, quips, None)
    assert "volume=-6dB:enable" in graph and "loudnorm=I=-18:" in graph


def test_every_spoken_quip_is_normalized_before_the_delay_and_silent_ones_are_not_inputs():
    graph = graph_of([spoken(1.0, 2.0, "a"), silent(5.0), spoken(9.0, 1.0, "b")])
    assert graph.count("loudnorm") == 2
    assert all(c.index("acompressor") < c.index("loudnorm") < c.index("adelay") for c in graph.splitlines() if "adelay" in c)


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


# ---- 9:16 and trim -----------------------------------------------------------------------------


def test_defaults_are_untouched_by_the_new_options():
    plain = args_for([spoken(1.0, 2.0)])
    assert "-ss" not in plain and plain[plain.index("-i") + 1] == "C:/up/v.mp4"
    assert "crop=" not in graph_of([spoken(1.0, 2.0)])
    assert export_module.export_name(True) == "captions1.mp4" and export_module.export_name(False) == "captions0.mp4"
    quips = [spoken(1.0, 2.0)]
    assert export_module.select_trim_quips(quips, None, None) is quips


def test_vertical_crop_centers_on_the_motion_box_and_clamps_to_the_frame():
    # 1920x1080: the window is 608x1080 (even), not scaled because the source is shorter than 1920
    x, y, cw, ch, ow, oh = export_module.vertical_crop(1920, 1080, (0.6, 0.2, 0.8, 0.9))
    assert (cw, ch, y, ow, oh) == (608, 1080, 0, 608, 1080)
    assert x == 1344 - 304  # box center 0.7 * 1920 = 1344
    assert export_module.vertical_crop(1920, 1080, (0.0, 0.0, 0.1, 1.0))[0] == 0  # clamped at the left edge
    assert export_module.vertical_crop(1920, 1080, (0.95, 0.0, 1.0, 1.0))[0] == 1920 - 608  # and the right edge
    assert export_module.vertical_crop(1920, 1080, None)[0] == (1920 - 608) // 2  # no box: frame center


def test_vertical_crop_scales_a_tall_source_to_1080x1920_and_handles_portrait_input():
    x, y, cw, ch, ow, oh = export_module.vertical_crop(3840, 2160, (0.4, 0.0, 0.6, 1.0))
    assert (cw, ch, ow, oh) == (1216, 2160, 1080, 1920) and x % 2 == 0
    # already 9:16: the whole frame; narrower than 9:16: full width, a shorter window centered on the box vertically
    assert export_module.vertical_crop(1080, 1920, (0.4, 0.4, 0.6, 0.6)) == (0, 0, 1080, 1920, 1080, 1920)
    x, y, cw, ch, ow, oh = export_module.vertical_crop(720, 1600, (0.4, 0.8, 0.6, 1.0))
    assert (x, cw, ch, y) == (0, 720, 1280, 1600 - 1280)


def test_vertical_graph_crops_then_scales_then_burns_captions():
    window = (672, 0, 608, 1080, 608, 1080)
    graph = build_filter_graph(15.0, True, [spoken(1.0, 2.0)], "captions.ass", vertical=window)
    assert graph.splitlines()[0] == "[0:v]crop=608:1080:672:0,scale=608:1080:flags=lanczos,subtitles=captions.ass[vout];"
    assert "trunc(iw/2)" not in graph


def test_vertical_captions_sit_in_the_lower_third_with_a_vertical_font():
    wide = build_ass([spoken(1.0, 2.0)], 1920, 1080)
    tall = build_ass([spoken(1.0, 2.0)], 1080, 1920, vertical=True)

    def style(ass):
        fields = [line for line in ass.splitlines() if line.startswith("Style:")][0].split(",")
        return int(fields[2]), int(fields[-2])  # font size, bottom margin

    assert style(wide) == (round(1080 * 0.055), round(1080 * 0.06))
    size, margin = style(tall)
    assert size == round(1080 * 0.062) and margin == round(1920 * 0.2)
    assert 1920 - margin > 1920 * (2 / 3)  # the caption sits on the lower third


def test_trim_selects_quips_that_start_inside_and_finish_before_end_and_shifts_them():
    quips = [spoken(2.0, 1.0, "a"), spoken(10.0, 2.0, "b"), spoken(14.0, 2.0, "c"), spoken(20.0, 1.0, "d"), silent(14.5, "s")]
    kept = export_module.select_trim_quips(quips, 5.0, 15.0)
    assert [(q.text, q.timestamp) for q in kept] == [("line b", 5.0)]  # c would run to 16 and s to 17.5: dropped, not cut
    assert [q.text for q in export_module.select_trim_quips(quips, 5.0, None)] == ["line b", "line c", "line d", "s"]
    assert [q.timestamp for q in export_module.select_trim_quips(quips, None, 12.0)] == [2.0, 10.0]  # b ends exactly at 12: kept


def test_trimmed_args_seek_the_input_and_cap_the_length():
    args = build_ffmpeg_args(FFMPEG, "v.mp4", "o.mp4", 15.0, True, [spoken(1.0, 2.0)], start=20.0)
    assert args[args.index("-ss") + 1] == "20" and args.index("-ss") < args.index("-i")
    assert args[args.index("-i") - 2:args.index("-i")] == ["-t", "15"]
    assert args[-5:-3] == ["-t", "15"]  # the output length is the same


def test_normalize_range_validates_and_collapses_whole_clip_ends():
    norm = export_module.normalize_range
    assert norm(None, None, 40.0) == (None, None)
    assert norm(0.0, 40.0, 40.0) == (None, None)  # the whole clip: the same cache entry as an untrimmed export
    assert norm(5.0, 40.3, 40.0) == (5.0, None)  # within the player's rounding
    assert norm(10.0, 25.0, 40.0) == (10.0, 25.0)
    for bad in [(-1, 5), (10, 10), (20, 10), (40, None), (50, None), (None, 41)]:
        with pytest.raises(ExportError) as err:
            norm(*bad, 40.0)
        assert err.value.status_code == 400


def test_cache_keys_differ_by_aspect_range_and_captions():
    name = export_module.export_name
    keys = {name(True), name(False), name(True, "9:16"), name(True, "9:16", 5.0), name(True, "9:16", 5.0, 20.5), name(True, "original", None, 20.5)}
    assert len(keys) == 6
    assert name(True, "9:16", 5.0, 20.5) == "captions1_9x16_s5_e20.5.mp4"
    assert ":" not in name(True, "9:16")


def test_route_rejects_bad_options_with_400_and_passes_good_ones(client, upload_dir, monkeypatch):
    job_id = completed_job(upload_dir)
    seen = []

    async def fake_render(jid, video_path, quips, captions, **options):
        seen.append(options)
        out = upload_dir / "out.mp4"
        out.write_bytes(b"x")
        return str(out)

    monkeypatch.setattr(main_module, "render_export", fake_render)
    for query in ["aspect=1:1", "aspect=", "start=5&end=5", "start=9&end=3", "start=-2", "end=nan"]:
        assert client.post(f"/api/jobs/{job_id}/export?{query}").status_code == 400, query
        assert client.get(f"/api/jobs/{job_id}/export.mp4?{query}").status_code == 400, query
    assert client.post(f"/api/jobs/{job_id}/export?start=abc").status_code == 422
    assert seen == []

    resp = client.post(f"/api/jobs/{job_id}/export?captions=1&aspect=9:16&start=2.5&end=17")
    assert resp.status_code == 200
    assert resp.json() == {"url": f"/api/jobs/{job_id}/export.mp4?captions=1&aspect=9%3A16&start=2.5&end=17"}
    assert seen == [{"aspect": "9:16", "start": 2.5, "end": 17.0}]
    resp = client.get(f"/api/jobs/{job_id}/export.mp4?aspect=9:16&start=2.5&end=17")
    assert "maneframe_clip_9x16_2.5-17s.mp4" in resp.headers["content-disposition"]
    client.post(f"/api/jobs/{job_id}/export")
    assert seen[-1] == {}  # defaults reach render_export exactly as before


@pytest.mark.anyio
async def test_render_rejects_a_range_outside_the_clip_before_ffmpeg(upload_dir, monkeypatch):
    video = upload_dir / "videos" / "v.mp4"
    video.parent.mkdir()
    video.write_bytes(b"x")
    monkeypatch.setattr(export_module, "_probe_video", lambda path: (320, 240, 10.0))
    for start, end in [(12.0, None), (None, 30.0), (4.0, 3.0)]:
        with pytest.raises(ExportError) as err:
            await export_module.render_export("j" * 32, str(video), [], True, start=start, end=end)
        assert err.value.status_code == 400
    with pytest.raises(ExportError) as err:
        await export_module.render_export("j" * 32, str(video), [], True, aspect="4:3")
    assert err.value.status_code == 400


@pytest.mark.skipif(not export_module.ffmpeg_available(), reason="no ffmpeg binary available")
@pytest.mark.anyio
async def test_real_vertical_trimmed_render_has_the_right_size_length_and_cache(upload_dir, monkeypatch):
    ffmpeg = export_module.resolve_ffmpeg()
    job_id = "c" * 32
    video = upload_dir / "videos" / f"{job_id}_wide.mp4"
    video.parent.mkdir()
    audio_dir = upload_dir / "audio" / job_id
    audio_dir.mkdir(parents=True)

    def run(*a):
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *a], check=True)

    run("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=12", "-f", "lavfi", "-i", "sine=frequency=220:duration=12",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video))
    run("-f", "lavfi", "-i", "sine=frequency=880:duration=1.5", str(audio_dir / "audio_5.00_ab.mp3"))
    quips = [
        {"timestamp": 5.0, "text": "Inside the trim", "audio_url": "http://h/uploads/audio/x/audio_5.00_ab.mp3", "duration_sec": 1.5},
        {"timestamp": 1.0, "text": "Before the trim", "audio_url": None, "duration_sec": None},
    ]
    # a narrow, steady subject: the crop window follows it; the fit framing has its own test
    monkeypatch.setattr(export_module.video_processor_service, "motion_track", lambda *a, **k: [(0.4, 0.2, 0.6, 0.8)] * 4)
    out = await export_module.render_export(job_id, str(video), quips, True, aspect="9:16", start=4.0, end=10.0)
    assert out == export_module.export_file(job_id, True, "9:16", 4.0, 10.0)
    cap = cv2.VideoCapture(out)
    assert (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))) == (202, 360)  # 9:16 of 360 high
    assert cap.get(cv2.CAP_PROP_FRAME_COUNT) == pytest.approx(60, abs=3)
    cap.release()
    probe = subprocess.run([ffmpeg, "-hide_banner", "-i", out], capture_output=True, text=True).stderr
    assert "Duration: 00:00:06.0" in probe and "Audio: aac" in probe
    assert await export_module.render_export(job_id, str(video), quips, True, aspect="9:16", start=4.0, end=10.0) == out  # cached
    assert await export_module.render_export(job_id, str(video), quips, True, aspect="9:16") != out


# ---- automatic framing (s51) ---------------------------------------------------------------------


def test_vertical_fit_canvas_is_9x16_without_upscaling():
    assert export_module.vertical_fit(1920, 1080)[4:] == (1080, 1920)
    assert export_module.vertical_fit(640, 360)[4:] == (640, 1138)


def test_fit_graph_blurs_a_copy_behind_the_whole_frame_and_burns_captions_on_top():
    graph = build_filter_graph(15.0, True, [spoken(1.0, 2.0)], "captions.ass", vertical=export_module.vertical_fit(1920, 1080), fit=True)
    lines = graph.split(";\n")
    assert lines[0] == "[0:v]split=2[bg][fg]"
    assert "boxblur" in lines[1] and "scale=1080:1920" in lines[1]
    assert lines[2].startswith("[fg]scale=1080:-2") and lines[2].endswith("overlay=(W-w)/2:(H-h)/2,subtitles=captions.ass[vout]")
    assert "crop=1920" not in graph


def test_pan_graph_uses_a_time_expression_for_crop_x():
    window = (0, 0, 608, 1080, 608, 1080)
    graph = build_filter_graph(10.0, False, [], None, vertical=window, pan_x="if(lt(t,2),0,100)")
    assert graph == "[0:v]crop=608:1080:x='if(lt(t,2),0,100)':y=0,scale=608:1080:flags=lanczos[vout]"


def test_captions_move_to_the_top_when_the_zone_chooser_says_so():
    def zone(start, end):
        return "top" if start >= 5 else "bottom"

    ass = build_ass([spoken(1.0, 2.0), spoken(6.0, 2.0)], 1080, 1920, vertical=True, zone_for=zone)
    dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert dialogue[0].split(",")[5:9] == ["0", "0", "0", ""] and r"{\an8}" not in dialogue[0]
    assert dialogue[1].split(",")[7] == "192" and r"{\an8}" in dialogue[1]
    assert r"{\an8}" not in build_ass([spoken(6.0, 2.0)], 1080, 1920, vertical=True)  # no chooser: unchanged


def test_y_mapper_puts_a_fit_video_in_the_middle_of_the_canvas():
    window = export_module.vertical_fit(1920, 1080)
    to_out = export_module.vertical_y_mapper("fit", 1920, 1080, window)
    assert to_out(0.0) == pytest.approx((1920 - 608) / 2 / 1920) and to_out(1.0) == pytest.approx(1 - (1920 - 608) / 2 / 1920)
    crop = export_module.vertical_crop(1920, 1080, None)
    assert export_module.vertical_y_mapper("crop", 1920, 1080, crop)(0.3) == pytest.approx(0.3)


@pytest.mark.skipif(not export_module.ffmpeg_available(), reason="no ffmpeg binary available")
@pytest.mark.anyio
async def test_real_fit_and_panning_renders_work_with_captions(upload_dir, monkeypatch):
    ffmpeg = export_module.resolve_ffmpeg()
    job_id = "d" * 32
    video = upload_dir / "videos" / f"{job_id}.mp4"
    video.parent.mkdir()
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    quips = [{"timestamp": 1.0, "text": "A caption", "audio_url": None, "duration_sec": None}]
    sizes = {}
    for name, boxes in {"fit": [(0.05, 0.4, 0.95, 0.9)] * 4, "crop": [(0.1, 0.1, 0.25, 0.3), (0.7, 0.1, 0.85, 0.3)] * 2}.items():
        monkeypatch.setattr(export_module.video_processor_service, "motion_track", lambda *a, _b=boxes, **k: _b)
        out = await export_module.render_export(job_id + name, str(video), quips, True, aspect="9:16")
        cap = cv2.VideoCapture(out)
        sizes[name] = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        cap.release()
    assert sizes == {"fit": (640, 1138), "crop": (202, 360)}


def test_top_caption_tag_is_a_backslash_not_a_control_character():
    ass = build_ass([spoken(6.0, 2.0)], 1080, 1920, vertical=True, zone_for=lambda start, end: "top")
    assert r"\an8" in ass and "\x07" not in ass


def test_ffmpeg_path_is_used_without_touching_imageio(monkeypatch, tmp_path):
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(settings, "FFMPEG_PATH", str(exe))
    import sys
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)  # importing it would raise
    assert export_module.resolve_ffmpeg() == str(exe)
