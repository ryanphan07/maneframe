"""Tests for packaging/launcher.py (the portable package's first-run chooser, downloader and helpers)."""
import hashlib
import importlib.util
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("launcher", ROOT / "packaging" / "launcher.py")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)

GB = launcher.GB
PAYLOAD = bytes(range(256)) * 4096 + b"tail"  # about 1 MB
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def quiet(*args, **kwargs):
    pass


# ---- recommendation ----

@pytest.mark.parametrize("ram,gpu,want", [
    (15.6 * GB, 23.8 * GB, "35"),   # the owner's PC: Radeon 860M, 15.6 GB RAM
    (16 * GB, None, "35"),
    (15 * GB, 0, "35"),
    (14.9 * GB, 8 * GB, "9"),
    (8 * GB, 4 * GB, "9"),
    (8 * GB, 16 * GB, "9"),         # GPU memory does not count
    (14.9 * GB, 23.8 * GB, "9"),    # an iGPU's reported shared memory does not count
    (None, None, "9"),              # nothing detected: the safe choice
])
def test_recommend(ram, gpu, want):
    assert launcher.recommend(None if ram is None else int(ram)) == want


MC9_DEVICES = ("Available devices:\n"
               "  Vulkan0: Intel(R) RaptorLake-S Mobile Graphics Controller (16237 MiB, 15469 MiB free)\n"
               "  Vulkan1: NVIDIA GeForce RTX 4060 Laptop GPU (7956 MiB, 5955 MiB free)\n")


def test_parse_devices_lists_every_device():
    assert launcher.parse_devices(MC9_DEVICES) == [
        ("Intel(R) RaptorLake-S Mobile Graphics Controller", 16237 * 1024 * 1024),
        ("NVIDIA GeForce RTX 4060 Laptop GPU", 7956 * 1024 * 1024)]
    one = "  Vulkan0: AMD Radeon(TM) 860M Graphics (24394 MiB, 23175 MiB free)\n"
    assert launcher.parse_devices(one) == [("AMD Radeon(TM) 860M Graphics", 24394 * 1024 * 1024)]
    assert launcher.parse_devices("no devices here") == []


def test_chooser_prints_one_line_per_gpu(tmp_path, monkeypatch):
    models = launcher.load_manifest(str(ROOT / "packaging" / "models.json"))
    monkeypatch.setattr(launcher, "free_disk_bytes", lambda p: 500 * GB)
    lines = []
    launcher.choose_model(models, 32 * GB, launcher.parse_devices(MC9_DEVICES), str(tmp_path), lambda _: "", lines.append)
    gpu_lines = [l for l in lines if l.strip().startswith("GPU:")]
    assert len(gpu_lines) == 2
    assert "Intel(R) RaptorLake-S Mobile Graphics Controller, 15.9 GB (as reported by llama-server)" in gpu_lines[0]
    assert "NVIDIA GeForce RTX 4060 Laptop GPU, 7.8 GB" in gpu_lines[1]


def test_llama_command_has_no_ngl():
    cmd = launcher.llama_command("m.gguf", "p.gguf", 8090)
    assert "-ngl" not in cmd and "--n-gpu-layers" not in cmd
    assert cmd[cmd.index("-m") + 1] == "m.gguf" and cmd[cmd.index("--mmproj") + 1] == "p.gguf"
    assert cmd[cmd.index("--port") + 1] == "8090" and "--jinja" in cmd


MC9_LOG = """load_tensors: loading model tensors, this can take a while... (mmap = true)
common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to 99, abort
ggml_vulkan: vk::Device::allocateMemory: ErrorOutOfDeviceMemory
failed to allocate Vulkan1 buffer of size 1019751936
"""


def test_load_failure_out_of_memory_points_to_change_model():
    msg = launcher.load_failure_message("x\n" * 100 + MC9_LOG, "Qwen3.6-35B-A3B", "L.log")
    assert msg == ("This PC could not load Qwen3.6-35B-A3B: not enough graphics memory. Double-click Change model.bat, "
                   "choose 9 (the smaller model), then start Maneframe again.")


def test_load_failure_out_of_memory_on_smallest_model_does_not_suggest_9():
    msg = launcher.load_failure_message(MC9_LOG, "Qwen3.5-9B", "L.log", smallest=True)
    assert msg == ("This PC could not load Qwen3.5-9B, the smallest model: not enough graphics memory. "
                   "Close other programs and start Maneframe again.")
    assert "choose 9" not in msg


def test_load_failure_other_error_prints_last_five_lines_and_path():
    log = "\n".join(f"line {i}" for i in range(50)) + "\nerror: model file is corrupt\n"
    msg = launcher.load_failure_message(log, "M", "C:\\logs\\llama-server.log")
    assert "model file is corrupt" in msg and "line 46" in msg and "line 45" not in msg
    assert "C:\\logs\\llama-server.log" in msg and "graphics memory" not in msg


def test_load_failure_oom_older_than_40_lines_is_ignored():
    log = MC9_LOG + "\n".join(f"ok {i}" for i in range(45))
    assert "graphics memory" not in launcher.load_failure_message(log, "M", "L")


# ---- manifest ----

def test_shipped_manifest_is_valid():
    models = launcher.load_manifest(str(ROOT / "packaging" / "models.json"))
    assert {m["key"] for m in models} == {"35", "9"}
    for m in models:
        assert m["license"] in ("apache-2.0", "mit")
        assert launcher.file_url(m, m["model"]).startswith(f"https://huggingface.co/{m['repo']}/resolve/{m['revision']}/")


def _manifest(tmp_path, mutate=None):
    models = json.loads((ROOT / "packaging" / "models.json").read_text(encoding="utf-8"))
    if mutate:
        mutate(models["models"])
    p = tmp_path / "models.json"
    p.write_text(json.dumps(models), encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("mutate", [
    lambda ms: ms[0].update(revision="main"),
    lambda ms: ms[0]["model"].update(sha256="abc"),
    lambda ms: ms[0]["model"].update(size=0),
    lambda ms: ms[0]["model"].update(file="sub/x.gguf"),
    lambda ms: ms[0].pop("repo"),
    lambda ms: ms[1].update(key=ms[0]["key"]),
    lambda ms: ms.clear(),
])
def test_manifest_rejects_bad_entries(tmp_path, mutate):
    with pytest.raises(launcher.Stop):
        launcher.load_manifest(_manifest(tmp_path, mutate))


def test_manifest_missing_or_not_json(tmp_path):
    with pytest.raises(launcher.Stop):
        launcher.load_manifest(str(tmp_path / "nope.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(launcher.Stop):
        launcher.load_manifest(str(bad))


# ---- disk space and chooser ----

def _entry(size=10 * GB):
    return {"id": "m", "key": "9", "name": "Test model", "repo": "a/b", "revision": "0" * 40, "license": "mit",
            "model": {"file": "m.gguf", "remote": "m.gguf", "size": size, "sha256": "0" * 64},
            "mmproj": {"file": "p.gguf", "remote": "p.gguf", "size": GB, "sha256": "0" * 64}}


def test_disk_refusal_says_how_much_is_needed(tmp_path):
    e = _entry()  # 11 GB download + 2 GB
    msg = launcher.disk_problem(e, str(tmp_path), 12 * GB)
    assert msg and "13.0 GB" in msg and "12.0 GB is free" in msg
    assert launcher.disk_problem(e, str(tmp_path), 13 * GB) is None


def test_disk_check_counts_partial_and_present_files(tmp_path):
    e = _entry()
    (tmp_path / "m.gguf.partial").write_bytes(b"x" * 1000)
    assert launcher.bytes_needed(e, str(tmp_path)) == 11 * GB - 1000
    (tmp_path / "p.gguf").write_bytes(b"p")
    assert launcher.bytes_needed(e, str(tmp_path)) == 10 * GB - 1000


def test_chooser_enter_takes_recommendation_and_override(tmp_path, monkeypatch):
    models = launcher.load_manifest(str(ROOT / "packaging" / "models.json"))
    monkeypatch.setattr(launcher, "free_disk_bytes", lambda p: 500 * GB)
    lines = []
    pick = launcher.choose_model(models, int(15.6 * GB), [("GPU X", int(23.8 * GB))], str(tmp_path), lambda _: "", lines.append)
    assert pick["key"] == "35"
    text = "\n".join(lines)
    assert "15.6 GB" in text and "GPU X" in text and "500.0 GB free" in text and "Recommended for this PC: 35" in text
    assert "download 16.5 GB: model 15.7 GB + vision 0.8 GB" in text
    answers = iter(["x", "9"])
    assert launcher.choose_model(models, int(15.6 * GB), [], str(tmp_path), lambda _: next(answers), quiet)["key"] == "9"


def test_chooser_refuses_a_model_that_does_not_fit_then_accepts_the_other(tmp_path, monkeypatch):
    models = launcher.load_manifest(str(ROOT / "packaging" / "models.json"))
    monkeypatch.setattr(launcher, "free_disk_bytes", lambda p: 10 * GB)  # fits the 9B (6.6 GB + 2), not the 35B
    lines = []
    answers = iter(["35", "9"])
    pick = launcher.choose_model(models, 32 * GB, [], str(tmp_path), lambda _: next(answers), lines.append)
    assert pick["key"] == "9"
    assert any("Not enough free disk space" in line for line in lines)


def test_chooser_without_keyboard_stops(tmp_path, monkeypatch):
    models = launcher.load_manifest(str(ROOT / "packaging" / "models.json"))
    monkeypatch.setattr(launcher, "free_disk_bytes", lambda p: 500 * GB)

    def eof(_):
        raise EOFError

    with pytest.raises(launcher.Stop):
        launcher.choose_model(models, 32 * GB, [], str(tmp_path), eof, quiet)


# ---- download against a local server with Range support ----

class RangeHandler(BaseHTTPRequestHandler):
    payload = PAYLOAD
    seen = []
    ignore_range = False
    cut_first = 0  # number of requests that send half the body and then close

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        cls.seen.append(self.headers.get("Range"))
        rng = self.headers.get("Range")
        start = 0
        if rng and not cls.ignore_range:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(self.payload):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}")
        else:
            self.send_response(200)
        body = self.payload[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if cls.cut_first > 0:
            cls.cut_first -= 1
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        self.wfile.write(body)


@pytest.fixture
def server():
    RangeHandler.seen = []
    RangeHandler.ignore_range = False
    RangeHandler.cut_first = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/f.bin"
    srv.shutdown()
    srv.server_close()


def test_range_header():
    assert launcher.range_header(0) is None
    assert launcher.range_header(1234) == "bytes=1234-"


def test_download_fresh(server, tmp_path):
    dest = str(tmp_path / "f.bin")
    launcher.download_file(server, dest, len(PAYLOAD), SHA, out=quiet)
    assert Path(dest).read_bytes() == PAYLOAD
    assert not os.path.exists(dest + ".partial")
    assert RangeHandler.seen == [None]


def test_download_resumes_from_partial(server, tmp_path):
    dest = str(tmp_path / "f.bin")
    Path(dest + ".partial").write_bytes(PAYLOAD[:300_000])
    launcher.download_file(server, dest, len(PAYLOAD), SHA, out=quiet)
    assert Path(dest).read_bytes() == PAYLOAD
    assert RangeHandler.seen == ["bytes=300000-"]


def test_download_restarts_when_server_ignores_range(server, tmp_path):
    RangeHandler.ignore_range = True
    dest = str(tmp_path / "f.bin")
    Path(dest + ".partial").write_bytes(PAYLOAD[:1000])
    launcher.download_file(server, dest, len(PAYLOAD), SHA, out=quiet)
    assert Path(dest).read_bytes() == PAYLOAD


def test_download_discards_oversized_partial(server, tmp_path):
    dest = str(tmp_path / "f.bin")
    Path(dest + ".partial").write_bytes(PAYLOAD + b"extra")
    launcher.download_file(server, dest, len(PAYLOAD), SHA, out=quiet)
    assert Path(dest).read_bytes() == PAYLOAD


def test_download_retries_a_dropped_connection_and_resumes(server, tmp_path):
    RangeHandler.cut_first = 1
    sleeps = []
    dest = str(tmp_path / "f.bin")
    launcher.download_file(server, dest, len(PAYLOAD), SHA, sleep=sleeps.append, backoff=2.0, out=quiet)
    assert Path(dest).read_bytes() == PAYLOAD
    assert sleeps == [2.0]
    assert RangeHandler.seen[0] is None and RangeHandler.seen[1].startswith("bytes=")


# ---- slow-connection reconnect (D16) ----

MB = launcher.MB


def test_is_slow_needs_a_full_window_of_slow_samples():
    assert not launcher.is_slow([])
    assert not launcher.is_slow([(0, 0), (20, 1 * MB)])           # slow, but only 20 s so far
    assert launcher.is_slow([(0, 0), (30, 30 * MB)])              # 1 MB/s for 30 s
    assert not launcher.is_slow([(0, 0), (30, 150 * MB)])         # exactly 5 MB/s is fine
    assert launcher.is_slow([(0, 0), (31, 31 * MB)])


def test_is_slow_ignores_a_short_dip():
    fast = [(t, t * 20 * MB) for t in range(0, 20)]               # 20 MB/s
    dip = [(t, 19 * 20 * MB + (t - 19) * MB) for t in range(20, 26)]  # 6 s at 1 MB/s
    assert not launcher.is_slow(fast + dip)
    # a long slow stretch after a fast start is caught once the last 30 s average under 5 MB/s
    slow = [(t, 19 * 20 * MB + (t - 19) * MB) for t in range(20, 80)]
    assert launcher.is_slow(fast + slow)


def test_format_size_uses_binary_units():
    assert launcher.format_size(int(15.7 * GB)) == "15.7 GB"
    assert launcher.format_size(GB) == "1.0 GB"
    assert launcher.format_size(1024 * MB - 1) == "1,024 MB"
    assert launcher.format_size(44 * MB) == "44 MB"
    assert "3.0 / 15.7 GB" in launcher.format_progress(3 * GB, int(15.7 * GB), 2 * MB)
    assert "44 / 100 MB" in launcher.format_progress(44 * MB, 100 * MB, 2 * MB)
    assert "2.0 MB/s" in launcher.format_progress(44 * MB, 100 * MB, 2 * MB)


class FakeResp:
    """Streams payload[start:] in 1 MB-like pieces, advancing a fake clock by `step` seconds per read."""

    def __init__(self, payload, start, status, clock, step, piece):
        self.data, self.pos, self.status, self.clock, self.step, self.piece = payload, start, status, clock, step, piece
        self.closed = False

    def read(self, n):
        self.clock.t += self.step
        block = self.data[self.pos:self.pos + self.piece]
        self.pos += len(block)
        return block

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True


class FakeClock:
    t = 1000.0

    def __call__(self):
        return self.t


def _fake_server(monkeypatch, payload, steps):
    """steps: seconds per read for the 1st, 2nd... connection (the last repeats). Returns the list of Range headers."""
    clock, seen, conns = FakeClock(), [], []

    def fake_urlopen(req, timeout=None):
        rng = req.headers.get("Range")
        seen.append(rng)
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        step = steps[min(len(seen) - 1, len(steps) - 1)]
        r = FakeResp(payload, start, 206 if rng else 200, clock, step, 100_000)
        conns.append(r)
        return r

    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)
    return clock, seen, conns


def test_slow_connection_is_replaced_and_resumes_at_the_right_byte(tmp_path, monkeypatch):
    payload = os.urandom(3_000_000)
    clock, seen, conns = _fake_server(monkeypatch, payload, [10.0, 0.001])  # 1st: 100 KB per 10 s; then fast
    lines = []
    dest = str(tmp_path / "f.bin")
    launcher.download_file("http://x/f.bin", dest, len(payload), hashlib.sha256(payload).hexdigest(),
                           out=lambda *a, **k: lines.append(a[0] if a else ""), sleep=lambda s: None, clock=clock)
    assert Path(dest).read_bytes() == payload
    assert len(seen) == 2 and seen[0] is None
    assert seen[1] == f"bytes={conns[0].pos}-" and 0 < conns[0].pos < len(payload)
    assert conns[0].closed
    assert sum("Slow connection, reconnecting (1 of 5)" in str(x) for x in lines) == 1


def test_reconnects_stop_after_five_then_the_connection_is_kept(tmp_path, monkeypatch):
    payload = os.urandom(2_000_000)
    clock, seen, conns = _fake_server(monkeypatch, payload, [10.0])  # every connection is slow
    lines = []
    dest = str(tmp_path / "f.bin")
    launcher.download_file("http://x/f.bin", dest, len(payload), hashlib.sha256(payload).hexdigest(),
                           out=lambda *a, **k: lines.append(a[0] if a else ""), sleep=lambda s: None, clock=clock)
    assert Path(dest).read_bytes() == payload
    assert len(seen) == 6                                  # 5 replaced connections, the 6th is kept to the end
    assert sum("Slow connection" in str(x) for x in lines) == 5
    assert not any("retry" in str(x) for x in lines)       # reconnects are not error retries


def test_download_gives_up_after_three_retries(tmp_path):
    sleeps = []
    with pytest.raises(launcher.Stop) as e:
        launcher.download_file("http://127.0.0.1:9/f.bin", str(tmp_path / "f.bin"), 10, SHA, sleep=sleeps.append, out=quiet)
    assert sleeps == [2.0, 4.0, 8.0]
    assert "3 retries" in str(e.value)


def test_download_checksum_mismatch_deletes_file_and_stops(server, tmp_path):
    dest = str(tmp_path / "f.bin")
    with pytest.raises(launcher.Stop) as e:
        launcher.download_file(server, dest, len(PAYLOAD), "0" * 64, out=quiet)
    assert "corrupt" in str(e.value)
    assert not os.path.exists(dest) and not os.path.exists(dest + ".partial")


# ---- ensure_model and config.json ----

def _real_entry():
    return {"id": "m", "key": "9", "name": "Test", "repo": "a/b", "revision": "0" * 40, "license": "mit",
            "model": {"file": "m.gguf", "remote": "m.gguf", "size": len(PAYLOAD), "sha256": SHA},
            "mmproj": {"file": "p.gguf", "remote": "p.gguf", "size": len(PAYLOAD), "sha256": SHA}}


def test_ensure_model_downloads_then_is_ready(server, tmp_path):
    e = _real_entry()
    data = str(tmp_path / "data")
    calls = []

    def dl(url, dest, size, sha, label=None, out=None):
        calls.append(dest)
        launcher.download_file(server, dest, size, sha, out=quiet)

    paths = launcher.ensure_model(e, data, out=quiet, downloader=dl)
    assert [os.path.basename(p) for p in paths] == ["m.gguf", "p.gguf"] and len(calls) == 2
    cfg = json.loads(Path(data, "config.json").read_text(encoding="utf-8"))
    assert cfg["model"] == "m" and set(cfg["verified"]) == {"m.gguf", "p.gguf"}
    assert launcher.model_ready(e, data)
    launcher.ensure_model(e, data, out=quiet, downloader=dl)  # nothing more to do
    assert len(calls) == 2


def test_existing_files_are_hashed_once(tmp_path, monkeypatch):
    e = _real_entry()
    data = str(tmp_path / "data")
    os.makedirs(os.path.join(data, "models"))
    for n in ("m.gguf", "p.gguf"):
        Path(data, "models", n).write_bytes(PAYLOAD)
    hashes = []
    real = launcher.sha256_file
    monkeypatch.setattr(launcher, "sha256_file", lambda *a, **k: hashes.append(a[0]) or real(*a, **k))

    def no_download(*a, **k):
        raise AssertionError("must not download")

    launcher.ensure_model(e, data, out=quiet, downloader=no_download)
    assert len(hashes) == 2
    launcher.ensure_model(e, data, out=quiet, downloader=no_download)
    assert len(hashes) == 2  # recorded in config.json, not hashed again


def test_existing_file_with_wrong_content_is_reported_not_deleted(tmp_path):
    e = _real_entry()
    data = str(tmp_path / "data")
    os.makedirs(os.path.join(data, "models"))
    bad = Path(data, "models", "m.gguf")
    bad.write_bytes(b"y" * len(PAYLOAD))  # right size, wrong content
    with pytest.raises(launcher.Stop):
        launcher.ensure_model(e, data, out=quiet)
    assert bad.exists()
    bad.write_bytes(b"short")
    with pytest.raises(launcher.Stop) as ex:
        launcher.ensure_model(e, data, out=quiet)
    assert "wrong size" in str(ex.value)


def test_clear_choice_keeps_verified_records(tmp_path):
    data = str(tmp_path)
    launcher.write_config(data, {"model": "m", "verified": {"m.gguf": {"size": 1, "sha256": "a"}}})
    launcher.clear_choice(data)
    assert launcher.read_config(data) == {"verified": {"m.gguf": {"size": 1, "sha256": "a"}}}


def test_read_config_tolerates_missing_and_garbage(tmp_path):
    assert launcher.read_config(str(tmp_path)) == {}
    (tmp_path / "config.json").write_text("not json", encoding="utf-8")
    assert launcher.read_config(str(tmp_path)) == {}


# ---- data_dir and free_port ----

def test_data_dir_uses_override_then_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("MANEFRAME_DATA_DIR", str(tmp_path / "x"))
    assert launcher.data_dir() == str(tmp_path / "x")
    monkeypatch.delenv("MANEFRAME_DATA_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert launcher.data_dir() == str(tmp_path / "local" / "Maneframe")


def test_free_port_skips_busy_and_avoided_ports():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    busy = s.getsockname()[1]
    try:
        assert launcher.port_in_use(busy)
        got = launcher.free_port(busy, avoid=(busy + 1,))
        assert got not in (busy, busy + 1) and got > busy
    finally:
        s.close()


def test_free_port_gives_up_clearly(monkeypatch):
    monkeypatch.setattr(launcher, "port_in_use", lambda p: True)
    with pytest.raises(launcher.Stop):
        launcher.free_port(9000)


# ---- ffmpeg first-run download (D18) ----

FF_EXE = "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
FF_BYTES = b"MZ fake ffmpeg " * 1000


def _wheel_bytes():
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("imageio_ffmpeg/__init__.py", "")
        z.writestr(FF_EXE, FF_BYTES)
    return buf.getvalue()


def _pin(wheel, sha=None):
    return {"version": "0.7.0", "url": "https://files.pythonhosted.org/x/imageio_ffmpeg-0.7.0-py3-none-win_amd64.whl",
            "size": len(wheel), "sha256": sha or hashlib.sha256(wheel).hexdigest(), "exe": FF_EXE}


def _serve_wheel(monkeypatch, wheel):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return FakeResp(wheel, 0, 200, FakeClock(), 0.0, 100_000)

    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_shipped_ffmpeg_pin_is_valid_and_matches_requirements():
    pin = launcher.load_ffmpeg_pin(str(ROOT / "packaging" / "ffmpeg.json"))
    assert pin["url"].startswith("https://files.pythonhosted.org/") and pin["url"].endswith("win_amd64.whl")
    assert f"imageio-ffmpeg=={pin['version']}" in (ROOT / "backend" / "requirements.txt").read_text()


def test_ffmpeg_wheel_is_downloaded_and_only_the_exe_extracted(tmp_path, monkeypatch):
    wheel = _wheel_bytes()
    calls = _serve_wheel(monkeypatch, wheel)
    exe = launcher.ensure_ffmpeg(str(tmp_path), _pin(wheel), quiet)
    assert exe == str(tmp_path / "tools" / "ffmpeg" / "ffmpeg-win-x86_64-v7.1.exe")
    assert Path(exe).read_bytes() == FF_BYTES
    assert sorted(os.listdir(tmp_path / "tools" / "ffmpeg")) == ["ffmpeg-win-x86_64-v7.1.exe"]  # wheel deleted
    assert len(calls) == 1


def test_ffmpeg_sha256_mismatch_stops_and_leaves_no_exe(tmp_path, monkeypatch):
    wheel = _wheel_bytes()
    _serve_wheel(monkeypatch, wheel)
    with pytest.raises(launcher.Stop, match="corrupt"):
        launcher.ensure_ffmpeg(str(tmp_path), _pin(wheel, sha="0" * 64), quiet)
    assert os.listdir(tmp_path / "tools" / "ffmpeg") == []


def test_existing_ffmpeg_skips_the_download(tmp_path, monkeypatch):
    wheel = _wheel_bytes()
    pin = _pin(wheel)
    exe = Path(launcher.ffmpeg_path(str(tmp_path), pin))
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    calls = _serve_wheel(monkeypatch, wheel)
    assert launcher.ensure_ffmpeg(str(tmp_path), pin, quiet) == str(exe)
    assert calls == [] and launcher.ffmpeg_bytes_needed(str(tmp_path), pin) == 0


def test_failed_ffmpeg_download_stops(tmp_path, monkeypatch):
    wheel = _wheel_bytes()

    def boom(req, timeout=None):
        raise OSError("offline")

    monkeypatch.setattr(launcher.urllib.request, "urlopen", boom)
    with pytest.raises(launcher.Stop, match="failed after"):
        launcher.ensure_ffmpeg(str(tmp_path), _pin(wheel), quiet,
                               downloader=lambda *a, **k: launcher.download_file(*a, **k, retries=0, sleep=lambda s: None))


def test_disk_check_includes_the_wheel_size(tmp_path):
    e = _entry(size=10 * GB)  # 11 GB of model files
    free = 13 * GB + 100
    assert launcher.disk_problem(e, str(tmp_path), free) is None
    assert launcher.disk_problem(e, str(tmp_path), free, extra=1 * GB) is not None
    assert launcher.ffmpeg_bytes_needed(str(tmp_path), {"size": 31247004, "exe": FF_EXE}) == 31247004


def test_ffmpeg_path_and_warmup_message_reach_the_backend(tmp_path, monkeypatch, capsys):
    envs = []

    class Proc:
        pid = 1
        returncode = None

        def __init__(self, cmd, **kw):
            envs.append(kw.get("env"))

        def poll(self):
            return None

    monkeypatch.setattr(launcher.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(launcher, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(launcher, "load_ffmpeg_pin", lambda: {"size": 1})
    monkeypatch.setattr(launcher, "ffmpeg_bytes_needed", lambda d, p: 0)
    monkeypatch.setattr(launcher, "select_model", lambda *a, **k: ("m.gguf", "p.gguf"))
    monkeypatch.setattr(launcher, "ensure_ffmpeg", lambda d, p: r"C:\data\tools\ffmpeg\ffmpeg.exe")
    monkeypatch.setattr(launcher, "free_port", lambda preferred, avoid=(): preferred)
    monkeypatch.setattr(launcher, "http_get", lambda url, timeout=3: (200, '{"status":"ok"}'))
    monkeypatch.setattr(launcher, "stop", lambda p: None)
    monkeypatch.setattr(launcher.KillOnClose, "add", lambda self, p: None)
    monkeypatch.setattr(launcher.subprocess, "Popen", Proc)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: None)
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *a, **k: FakeResp(b"", 0, 200, FakeClock(), 0, 1))

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher.time, "sleep", interrupt)
    assert launcher.main() == 0
    assert envs[1]["FFMPEG_PATH"] == r"C:\data\tools\ffmpeg\ffmpeg.exe"
    assert "Warming up the language model (the first start after a download can take a few minutes)..." in capsys.readouterr().out
