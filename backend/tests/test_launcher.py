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
    (8 * GB, 16 * GB, "35"),        # 16 GB of GPU memory alone is enough
    (None, None, "9"),              # nothing detected: the safe choice
])
def test_recommend(ram, gpu, want):
    assert launcher.recommend(None if ram is None else int(ram), None if gpu is None else int(gpu)) == want


def test_parse_devices_picks_the_largest():
    text = ("ggml_vulkan: Found 1 Vulkan devices:\nAvailable devices:\n"
            "  Vulkan0: AMD Radeon(TM) 860M Graphics (24394 MiB, 23175 MiB free)\n"
            "  Vulkan1: Other GPU (4096 MiB, 4000 MiB free)\n")
    assert launcher.parse_devices(text) == ("AMD Radeon(TM) 860M Graphics", 24394 * 1024 * 1024)
    assert launcher.parse_devices("no devices here") == (None, None)


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
    pick = launcher.choose_model(models, int(15.6 * GB), "GPU X", int(23.8 * GB), str(tmp_path), lambda _: "", lines.append)
    assert pick["key"] == "35"
    text = "\n".join(lines)
    assert "15.6 GB" in text and "GPU X" in text and "500.0 GB free" in text and "Recommended for this PC: 35" in text
    answers = iter(["x", "9"])
    assert launcher.choose_model(models, int(15.6 * GB), "GPU X", None, str(tmp_path), lambda _: next(answers), quiet)["key"] == "9"


def test_chooser_refuses_a_model_that_does_not_fit_then_accepts_the_other(tmp_path, monkeypatch):
    models = launcher.load_manifest(str(ROOT / "packaging" / "models.json"))
    monkeypatch.setattr(launcher, "free_disk_bytes", lambda p: 10 * GB)  # fits the 9B (6.6 GB + 2), not the 35B
    lines = []
    answers = iter(["35", "9"])
    pick = launcher.choose_model(models, 32 * GB, None, None, str(tmp_path), lambda _: next(answers), lines.append)
    assert pick["key"] == "9"
    assert any("Not enough free disk space" in line for line in lines)


def test_chooser_without_keyboard_stops(tmp_path, monkeypatch):
    models = launcher.load_manifest(str(ROOT / "packaging" / "models.json"))
    monkeypatch.setattr(launcher, "free_disk_bytes", lambda p: 500 * GB)

    def eof(_):
        raise EOFError

    with pytest.raises(launcher.Stop):
        launcher.choose_model(models, 32 * GB, None, None, str(tmp_path), eof, quiet)


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
