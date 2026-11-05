"""Benchmark the running backend on real clips and append a dated section to docs/BENCHMARKS.md.

Run from backend/ with the venv active and uvicorn already serving:
    python scripts/benchmark.py [--log ../logs/backend.log.err] [--warm-first] [--style deadpan]

--log defaults to logs/backend.log.err, where start.ps1 sends uvicorn's output (the logging module writes to stderr).
If the file you pass has no job lines but "<file>.err" does (e.g. logs/backend.log), the .err file is used.

Window timings and the Shifting/Dropping/TTS-failed counts come from the backend log. The per-window LLM lines
carry the job id ("LLM window 1/2 (job <id>): ..."), so they are attributed by it. The other lines have no id, so
they (and LLM lines from older logs) belong to the job whose "Created job <id>" line precedes them (jobs run one at a
time). Status times are polled every POLL_SECONDS, so phase seconds are accurate to about that.
"""
import argparse
import datetime
import importlib.metadata
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

POLL_SECONDS = 2.0
JOB_TIMEOUT_SECONDS = 15 * 60
PHASES = ["extracting", "analyzing", "synthesizing"]
OVERLAP_TOLERANCE = 0.05
SETTING_KEYS = [
    "LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT", "LLM_ENABLE_THINKING", "LLM_MAX_TOKENS", "LLM_TEMPERATURE",
    "WINDOW_SECONDS", "FRAMES_PER_WINDOW", "FRAME_WIDTH", "MAX_QUIPS_PER_WINDOW", "MIN_QUIP_GAP_SEC",
    "TTS_ENGINE", "TTS_VOICE", "KOKORO_VOICE", "KOKORO_SPEED", "TTS_RATE", "TTS_ENABLED", "TTS_TIMEOUT_SECONDS", "TTS_CONCURRENCY", "TTS_START_STAGGER_SECONDS",
    "TTS_RETRIES", "MAX_SHIFT_SEC", "MAX_VIDEO_SECONDS",
]
DEFAULT_CLIPS = [
    r"C:\Users\ryanp\Downloads\shorter_test.mp4",
    r"C:\Users\ryanp\Downloads\hoco2026.MOV",
    r"C:\Users\ryanp\Downloads\hoco_40s.mp4",
]

CREATED_RE = re.compile(r"Created job (\w+)")
LLM_RE = re.compile(
    r"LLM (?P<label>.+?)(?: \(job (?P<job>\w+)\))?: frames=(?P<frames>\d+) prompt_tokens=(?P<pt>\S+) completion_tokens=(?P<ct>\S+) seconds=(?P<sec>[\d.]+)"
)


def resolve_log(arg: Optional[str]) -> Path:
    """The backend log to parse: --log if given, else start.ps1's logs/backend.log.err. Falls back to "<file>.err"
    when the given file has no job lines (start.ps1 keeps stdout in backend.log and uvicorn's logging in .err)."""
    path = Path(arg) if arg else REPO_DIR / "logs" / "backend.log.err"

    def has_jobs(p: Path) -> bool:
        try:
            return "Created job" in p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    alt = path.with_name(path.name + ".err")
    if not has_jobs(path) and has_jobs(alt):
        return alt
    return path


def parse_log(text: str) -> Dict[str, Dict[str, Any]]:
    """Per job id: LLM windows (label, prompt/completion tokens, seconds) and counts of Shifting/Dropping/TTS failed."""
    jobs: Dict[str, Dict[str, Any]] = {}
    current: Optional[Dict[str, Any]] = None

    def new_job() -> Dict[str, Any]:
        return {"windows": [], "shifting": 0, "dropping": 0, "tts_failed": 0}

    for line in text.splitlines():
        created = CREATED_RE.search(line)
        if created:
            current = jobs.setdefault(created.group(1), new_job())
            continue
        llm = LLM_RE.search(line)
        if llm:
            target = jobs.setdefault(llm.group("job"), new_job()) if llm.group("job") else current
            if target is None:
                continue
            def tok(v: str) -> Optional[int]:
                return int(v) if v.isdigit() else None
            target["windows"].append({
                "label": llm.group("label"), "prompt_tokens": tok(llm.group("pt")),
                "completion_tokens": tok(llm.group("ct")), "seconds": float(llm.group("sec")),
            })
        if current is None:
            continue
        if "Shifting quip" in line:
            current["shifting"] += 1
        if "Dropping quip" in line:
            current["dropping"] += 1
        if "TTS failed" in line:
            current["tts_failed"] += 1
    return jobs


def count_overlaps(quips: List[Dict[str, Any]], tolerance: float = OVERLAP_TOLERANCE) -> int:
    """Pairs of consecutive audio quips where timestamp + duration_sec runs past the next timestamp."""
    audio = sorted(
        (q for q in quips if q.get("audio_url") and q.get("duration_sec")), key=lambda q: q["timestamp"]
    )
    return sum(
        1 for a, b in zip(audio, audio[1:]) if a["timestamp"] + a["duration_sec"] > b["timestamp"] + tolerance
    )


def phase_seconds(first_seen: Dict[str, float]) -> Dict[str, Optional[float]]:
    """Seconds spent in each phase: time from its first appearance to the next status's first appearance."""
    order = [s for s in PHASES + ["completed"] if s in first_seen]
    out: Dict[str, Optional[float]] = {p: None for p in PHASES}
    for cur, nxt in zip(order, order[1:]):
        if cur in out:
            out[cur] = first_seen[nxt] - first_seen[cur]
    return out


def summarize_quips(quips: List[Dict[str, Any]]) -> Dict[str, Any]:
    with_audio = [q for q in quips if q.get("audio_url")]
    return {
        "total": len(quips),
        "audio": len(with_audio),
        "no_audio": len(quips) - len(with_audio),
        "spoken": sum(q.get("duration_sec") or 0 for q in with_audio),
        "overlaps": count_overlaps(quips),
    }


def _fmt(v: Optional[float], digits: int = 1) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def format_row(r: Dict[str, Any]) -> List[str]:
    q = r.get("quips")
    windows = r.get("windows")
    if windows:
        secs = [w["seconds"] for w in windows]
        pt = [w["prompt_tokens"] for w in windows if w["prompt_tokens"] is not None]
        ct = [w["completion_tokens"] for w in windows if w["completion_tokens"] is not None]
        win = f"{len(secs)} / {sum(secs) / len(secs):.1f} / {max(secs):.1f}"
        tokens = f"{sum(pt) // max(len(pt), 1)} / {sum(ct) // max(len(ct), 1)}"
    else:
        win, tokens = "n/a", "n/a"
    ph = r["phases"]
    return [
        r["label"],
        f"{_fmt(r.get('clip_seconds'))} s / {r['size_mb']:.1f} MB",
        _fmt(r.get("total_seconds")),
        f"{_fmt(ph.get('extracting'))} / {_fmt(ph.get('analyzing'))} / {_fmt(ph.get('synthesizing'))}",
        f"{r['status']}: {r['message']}" if r["status"] != "completed" else "completed",
        f"{q['total']} / {q['audio']} / {q['no_audio']}" if q else "n/a",
        _fmt(q["spoken"]) if q else "n/a",
        str(q["overlaps"]) if q else "n/a",
        win,
        tokens,
        f"{r['shifting']} / {r['dropping']} / {r['tts_failed']}" if r.get("shifting") is not None else "n/a",
    ]


TABLE_HEADER = [
    "Run", "Clip (length / size)", "Total s", "Extract / analyze / synth s", "Final status",
    "Quips (total / audio / no audio)", "Spoken s", "Audio overlaps",
    "LLM calls (incl. retries) / avg s / max s", "Avg prompt / completion tokens", "Shifting / Dropping / TTS failed",
]


def format_table(rows: List[Dict[str, Any]]) -> str:
    lines = ["| " + " | ".join(TABLE_HEADER) + " |", "|" + "---|" * len(TABLE_HEADER)]
    for r in rows:
        lines.append("| " + " | ".join(c.replace("|", "/") for c in format_row(r)) + " |")
    return "\n".join(lines)


def format_section(date: str, env: Dict[str, str], rows: List[Dict[str, Any]], notes: List[str]) -> str:
    out = [f"## {date}", "", "### Environment", ""]
    out += [f"- {k}: {v}" for k, v in env.items()]
    out += ["", "### Results", "", format_table(rows), ""]
    out += notes
    return "\n".join(out).rstrip() + "\n"


def append_section(path: Path, section: str) -> None:
    header = "# Benchmarks\n\nMeasured by `backend/scripts/benchmark.py` against a running backend. Each run appends a dated section; earlier sections are never overwritten.\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else header
    path.write_text(existing.rstrip() + "\n\n" + section, encoding="utf-8")


def run_clip(client, base_url: str, clip: Path, label: str, style: Optional[str] = None, timeout_seconds: float = JOB_TIMEOUT_SECONDS) -> Dict[str, Any]:
    from app.services.video_processor import get_video_duration

    result: Dict[str, Any] = {
        "label": label, "clip": clip.name, "size_mb": clip.stat().st_size / 1e6,
        "clip_seconds": None, "phases": {}, "status": "error", "message": "", "total_seconds": None, "job_id": None,
    }
    try:
        result["clip_seconds"] = get_video_duration(str(clip))
    except Exception as exc:
        print(f"  could not read duration: {exc}")
    with clip.open("rb") as fh:
        resp = client.post(f"{base_url}/api/upload", files={"file": (clip.name, fh)}, data={"style": style} if style else None, timeout=600)
    t0 = time.monotonic()
    if resp.status_code != 200:
        result["message"] = f"upload rejected {resp.status_code}: {resp.text[:120]}"
        return result
    job_id = resp.json()["job_id"]
    result["job_id"] = job_id
    first_seen: Dict[str, float] = {}
    job: Dict[str, Any] = {}
    while time.monotonic() - t0 < timeout_seconds:
        job = client.get(f"{base_url}/api/jobs/{job_id}", timeout=30).json()
        now = time.monotonic() - t0
        if job["status"] not in first_seen:
            first_seen[job["status"]] = now
            print(f"  +{now:6.1f}s {job['status']} ({job.get('message')})")
        if "first_partial_seconds" not in result and (job.get("result") or {}).get("partial"):
            result["first_partial_seconds"] = now  # first playable quip (a long clip's wait before anything shows)
            print(f"  +{now:6.1f}s first partial result")
        if job["status"] in ("completed", "error"):
            break
        time.sleep(POLL_SECONDS)
    else:
        result["message"] = f"timed out after {int(timeout_seconds) // 60} min (last status {job.get('status')})"
        result["status"] = "timeout"
        result["phases"] = phase_seconds(first_seen)
        return result
    result["status"] = job["status"]
    result["message"] = job.get("message") or ""
    result["total_seconds"] = first_seen[job["status"]]
    result["phases"] = phase_seconds(first_seen)
    quips = (job.get("result") or {}).get("quips")
    if quips is not None:
        result["quips"] = summarize_quips(quips)
    return result


def environment(base_url: str, llm_base: str, client) -> Dict[str, str]:
    from app.config import settings

    try:
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_DIR, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_DIR, capture_output=True, text=True).stdout.strip()
        git += " (uncommitted changes)" if dirty else ""
    except Exception:
        git = "unknown"
    try:
        model = client.get(f"{llm_base}/models", timeout=10).json()["data"][0]["id"]
    except Exception as exc:
        model = f"unknown ({exc})"
    try:
        edge = importlib.metadata.version("edge-tts")
    except importlib.metadata.PackageNotFoundError:
        edge = "not installed"
    env = {
        "Date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Git commit": git,
        "Model (GET /v1/models)": model,
        "edge-tts": edge,
        "Python": platform.python_version(),
        "OS": platform.platform(),
        "Settings (as loaded by this script from backend/.env; the server must have been started from the same folder)":
            ", ".join(f"{k}={getattr(settings, k, '?')}" for k in SETTING_KEYS),
    }
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clips", nargs="+", default=DEFAULT_CLIPS)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--out", default=str(REPO_DIR / "docs" / "BENCHMARKS.md"))
    parser.add_argument("--log", default=None, help="backend log file (default logs/backend.log.err, what start.ps1 writes), for window timings and Shifting/Dropping/TTS failed counts")
    parser.add_argument("--timeout-min", type=float, default=JOB_TIMEOUT_SECONDS / 60, help="give up on a clip after this many minutes (long clips need more than the default 15)")
    parser.add_argument("--style", default=None, help="commentary style sent with each upload (server default when omitted)")
    parser.add_argument("--warm-first", action="store_true", help='run the first clip again at the end, labelled "warm (prompt cache)"')
    args = parser.parse_args()

    import httpx
    from app.config import settings

    plan = [(Path(c), Path(c).name) for c in args.clips]
    if args.warm_first and plan:
        plan.append((plan[0][0], f"{plan[0][1]} - warm (prompt cache)"))
    missing = [str(p) for p, _ in plan if not p.is_file()]
    if missing:
        print("Missing clips:", *missing, sep="\n  ")
        return 2

    rows: List[Dict[str, Any]] = []
    with httpx.Client() as client:
        env = environment(args.base_url, settings.LLM_BASE_URL.rstrip("/"), client)
        for clip, label in plan:
            print(f"== {label}")
            rows.append(run_clip(client, args.base_url, clip, label, args.style, args.timeout_min * 60))
            print(f"  -> {rows[-1]['status']} {rows[-1]['message']}")

    notes = [
        f"Style: {args.style or 'server default'}.",
        f"Phase seconds come from polling every {POLL_SECONDS:.0f} s (about +/-{POLL_SECONDS:.0f} s). Total is wall clock from upload accepted to the first poll that saw `completed`/`error`. "
        "Overlap = consecutive audio quips where timestamp + duration_sec exceeds the next timestamp by more than 0.05 s.",
    ]
    log_path = resolve_log(args.log)
    if log_path.is_file():
        log_jobs = parse_log(log_path.read_text(encoding="utf-8", errors="replace"))
        for r in rows:
            info = log_jobs.get(r["job_id"] or "")
            if info:
                r.update(windows=info["windows"], shifting=info["shifting"], dropping=info["dropping"], tts_failed=info["tts_failed"])
        notes.append(f"Window, token and Shifting/Dropping/TTS failed columns are from `{log_path.name}`; n/a means the job was not found in the log.")
    else:
        notes.append(f"Backend log `{log_path}` not found: window timings and log counts are n/a.")
    per_window = [
        f"- {r['label']}: " + ", ".join(f"{w['seconds']:.1f}s" for w in r["windows"])
        for r in rows if r.get("windows")
    ]
    if per_window:
        notes += ["", "Per-window LLM seconds:", ""] + per_window

    section = format_section(env["Date"], env, rows, notes)
    append_section(Path(args.out), section)
    print(f"Appended section to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
