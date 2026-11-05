import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("benchmark", Path(__file__).resolve().parent.parent / "scripts" / "benchmark.py")
benchmark = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(benchmark)

LOG = """\
2026-10-28 10:00:00,000 INFO maneframe.jobs: Created job aaa111
2026-10-28 10:00:05,000 INFO maneframe.llm: LLM window 1/2: frames=6 prompt_tokens=3248 completion_tokens=120 seconds=44.7
2026-10-28 10:00:50,000 INFO maneframe.llm: LLM window 2/2: frames=6 prompt_tokens=3300 completion_tokens=? seconds=20.0
2026-10-28 10:01:10,000 INFO maneframe.commentary: Shifting quip from t=5.00 to t=6.00 to avoid audio overlap
2026-10-28 10:01:11,000 WARNING maneframe.commentary: Dropping quip at t=9.00: would need a 3.00s shift
2026-10-28 10:01:12,000 WARNING maneframe.commentary: TTS failed for t=2.00: boom
2026-10-28 10:02:00,000 INFO maneframe.jobs: Created job bbb222
2026-10-28 10:02:05,000 INFO maneframe.llm: LLM window 1/1: frames=6 prompt_tokens=3000 completion_tokens=90 seconds=10.5
2026-10-28 10:02:06,000 INFO maneframe.commentary: Shifting quip from t=1.00 to t=1.50 to avoid audio overlap
"""


def test_parse_log_splits_by_job():
    jobs = benchmark.parse_log("stray line\n" + LOG)
    assert set(jobs) == {"aaa111", "bbb222"}
    a = jobs["aaa111"]
    assert [w["seconds"] for w in a["windows"]] == [44.7, 20.0]
    assert a["windows"][0]["prompt_tokens"] == 3248
    assert a["windows"][1]["completion_tokens"] is None
    assert (a["shifting"], a["dropping"], a["tts_failed"]) == (1, 1, 1)
    assert (jobs["bbb222"]["shifting"], jobs["bbb222"]["dropping"], jobs["bbb222"]["tts_failed"]) == (1, 0, 0)


def test_parse_log_empty():
    assert benchmark.parse_log("") == {}


def _q(t, d, audio=True):
    return {"timestamp": t, "text": "x", "audio_url": "/a.mp3" if audio else None, "duration_sec": d if audio else None}


def test_count_overlaps():
    assert benchmark.count_overlaps([_q(0, 2), _q(2.3, 2), _q(5, 1)]) == 0
    assert benchmark.count_overlaps([_q(0, 3), _q(2.5, 1)]) == 1
    # within the 0.05 s tolerance is not an overlap
    assert benchmark.count_overlaps([_q(0, 2.04), _q(2, 1)]) == 0
    # a quip without audio never overlaps and is skipped when pairing
    assert benchmark.count_overlaps([_q(0, 2), _q(1, 0, audio=False), _q(2.5, 1)]) == 0
    assert benchmark.count_overlaps([_q(2.5, 1), _q(0, 3)]) == 1


def test_phase_seconds_and_summary():
    ph = benchmark.phase_seconds({"uploading": 0.0, "extracting": 1.0, "analyzing": 3.0, "synthesizing": 63.0, "completed": 70.0})
    assert ph == {"extracting": 2.0, "analyzing": 60.0, "synthesizing": 7.0}
    partial = benchmark.phase_seconds({"extracting": 1.0, "analyzing": 3.0, "error": 9.0})
    assert partial["analyzing"] is None and partial["extracting"] == 2.0
    s = benchmark.summarize_quips([_q(0, 2), _q(3, 1.5), _q(6, 0, audio=False)])
    assert (s["total"], s["audio"], s["no_audio"], s["spoken"], s["overlaps"]) == (3, 2, 1, 3.5, 0)


def test_format_table_and_section(tmp_path):
    ok = {
        "label": "clip.mp4", "clip_seconds": 20.0, "size_mb": 12.34, "total_seconds": 95.0,
        "phases": {"extracting": 2.0, "analyzing": 80.0, "synthesizing": 13.0}, "status": "completed", "message": "Done",
        "quips": {"total": 4, "audio": 3, "no_audio": 1, "spoken": 7.5, "overlaps": 0},
        "windows": [{"seconds": 40.0, "prompt_tokens": 3000, "completion_tokens": 100}, {"seconds": 42.0, "prompt_tokens": 3200, "completion_tokens": 120}],
        "shifting": 1, "dropping": 0, "tts_failed": 2,
    }
    bad = {"label": "bad.mov", "clip_seconds": None, "size_mb": 1.0, "total_seconds": None, "phases": {}, "status": "error", "message": "boom | x"}
    table = benchmark.format_table([ok, bad])
    lines = table.splitlines()
    assert len(lines) == 4 and all(l.count("|") == lines[0].count("|") for l in lines)
    assert "20.0 s / 12.3 MB" in lines[2] and "4 / 3 / 1" in lines[2] and "2 / 41.0 / 42.0" in lines[2] and "1 / 0 / 2" in lines[2]
    assert "error: boom / x" in lines[3] and "n/a" in lines[3]
    out = tmp_path / "B.md"
    benchmark.append_section(out, benchmark.format_section("2026-10-28", {"Model": "m"}, [ok], ["note"]))
    benchmark.append_section(out, benchmark.format_section("2026-10-29", {"Model": "m"}, [ok], []))
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Benchmarks") and "## 2026-10-28" in text and "## 2026-10-29" in text and "- Model: m" in text


def test_parse_log_attributes_llm_lines_by_job_id():
    log = (
        "INFO maneframe.jobs: Created job aaa111\n"
        "INFO maneframe.jobs: Created job bbb222\n"
        "INFO maneframe.llm: LLM window 1/1 (job aaa111): frames=6 prompt_tokens=10 completion_tokens=5 seconds=3.0\n"
        "INFO maneframe.llm: LLM window 1/1 (job bbb222): frames=6 prompt_tokens=10 completion_tokens=5 seconds=4.0\n"
    )
    jobs = benchmark.parse_log(log)
    assert [w["seconds"] for w in jobs["aaa111"]["windows"]] == [3.0]
    assert [w["seconds"] for w in jobs["bbb222"]["windows"]] == [4.0]
    assert jobs["aaa111"]["windows"][0]["label"] == "window 1/1"


def test_resolve_log_default_and_err_fallback(tmp_path):
    out, err = tmp_path / "backend.log", tmp_path / "backend.log.err"
    out.write_text("uvicorn stdout, no job lines")
    err.write_text(LOG)
    assert benchmark.resolve_log(str(out)) == err  # start.ps1 stdout file -> its .err sibling
    assert benchmark.resolve_log(str(err)) == err
    other = tmp_path / "other.log"
    other.write_text(LOG)
    assert benchmark.resolve_log(str(other)) == other
    assert benchmark.resolve_log(None).name == "backend.log.err"
