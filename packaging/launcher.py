"""Starts Maneframe from the portable package: the model chooser/downloader on first run, then llama-server, the backend and the browser.

Runs with the package's own Python (python\\python.exe), standard library only. Layout next to this file:
python\\ (private Python), backend\\app, frontend\\dist, llama\\llama-server.exe. Data lives outside the package
(MANEFRAME_DATA_DIR, else %LOCALAPPDATA%\\Maneframe). Ctrl+C, closing the window or a crash stops both servers.
"""
import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from ctypes import wintypes

ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(ROOT, "python", "python.exe")
LLAMA = os.path.join(ROOT, "llama", "llama-server.exe")
BACKEND_DIR = os.path.join(ROOT, "backend")

MANIFEST = os.path.join(ROOT, "models.json")
FFMPEG_PIN = os.path.join(ROOT, "ffmpeg.json")
LLM_CTX = "8192"  # measured prompts are about 5.1K to 6.4K tokens; the rest of the 8192 is the answer
MB = 1024 ** 2
GB = 1024 ** 3
RECOMMEND_35B_RAM = 15 * GB   # total RAM at which the 35B is recommended (GPU memory is not used, D13)
DISK_MARGIN = 2 * GB          # free space required beyond the download itself
DOWNLOAD_RETRIES = 3
SLOW_MIN_RATE = 5 * MB        # a connection averaging less than this over SLOW_WINDOW is dropped and resumed (D16)
SLOW_WINDOW = 30              # seconds
SLOW_RECONNECTS = 5           # per file; these do not count against DOWNLOAD_RETRIES
WARMUP_TIMEOUT = 300          # seconds; measured 6 s with a warm file cache, but 120 s was not enough on the first run after a download (MC-8)
PREFERRED_LLAMA_PORT = 8080
PREFERRED_BACKEND_PORT = 8000
LLAMA_READY_TIMEOUT = 600
BACKEND_READY_TIMEOUT = 90


class Stop(Exception):
    """A clean, explained exit: the message is printed, the exit code is non-zero."""


def data_dir():
    base = os.environ.get("MANEFRAME_DATA_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"), "Maneframe")
    return os.path.abspath(base)


def port_in_use(port):
    """True when something listens on the port, whatever address it bound (Docker binds 0.0.0.0)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        pass
    probe = socket.socket()
    try:
        probe.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def free_port(preferred, avoid=()):
    for port in range(preferred, preferred + 200):
        if port not in avoid and not port_in_use(port):
            return port
    raise Stop(f"No free port found from {preferred} to {preferred + 199}.")


def http_get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception:
        return None, ""


class KillOnClose:
    """A Windows job object: every process added dies when this launcher dies, however it dies (window closed, crash)."""

    def __init__(self):
        self.handle = None
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.k32 = k32
            self.handle = k32.CreateJobObjectW(None, None)

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]

            class Extended(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = Extended()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        except Exception:
            self.handle = None  # the finally block in main() still stops both servers on Ctrl+C

    def add(self, proc):
        if self.handle:
            self.k32.AssignProcessToJobObject(self.handle, int(proc._handle))


# ---- Model manifest, hardware detection, chooser ----

def load_manifest(path=MANIFEST):
    """Reads and validates models.json. Raises Stop with a clear message when it is unusable."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise Stop(f"models.json is missing or unreadable ({e}). Unzip the whole package again.")
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list) or not models:
        raise Stop("models.json lists no models.")
    seen = set()
    for m in models:
        try:
            for k in ("id", "key", "name", "repo", "revision", "license"):
                if not isinstance(m[k], str) or not m[k]:
                    raise ValueError(k)
            if not re.fullmatch(r"[0-9a-f]{40}", m["revision"]):
                raise ValueError("revision must be a 40-character commit hash")
            for part in ("model", "mmproj"):
                f = m[part]
                for k in ("file", "remote"):
                    if not isinstance(f[k], str) or not f[k]:
                        raise ValueError(f"{part}.{k}")
                if "/" in f["file"] or "\\" in f["file"]:
                    raise ValueError(f"{part}.file must be a plain file name")
                if not isinstance(f["size"], int) or f["size"] <= 0:
                    raise ValueError(f"{part}.size")
                if not re.fullmatch(r"[0-9a-f]{64}", str(f["sha256"])):
                    raise ValueError(f"{part}.sha256")
        except (KeyError, TypeError, ValueError) as e:
            raise Stop(f"models.json has a bad entry ({m.get('id') if isinstance(m, dict) else m}): {e}")
        if m["id"] in seen or sum(1 for x in models if x["key"] == m["key"]) > 1:
            raise Stop(f"models.json repeats an id or key: {m['id']}")
        seen.add(m["id"])
    return models


def model_files(entry):
    return [entry["model"], entry["mmproj"]]


def download_size(entry):
    return sum(f["size"] for f in model_files(entry))


def file_url(entry, part):
    return f"https://huggingface.co/{entry['repo']}/resolve/{entry['revision']}/{part['remote']}"


def total_ram_bytes():
    """Total physical memory in bytes, or None when it cannot be read."""
    try:
        class Status(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]
        st = Status()
        st.dwLength = ctypes.sizeof(Status)
        if ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys)
    except Exception:
        pass
    return None


def parse_devices(text):
    """The largest device in `llama-server --list-devices` output: (name, bytes), or (None, None).

    Lines look like "  Vulkan0: AMD Radeon(TM) 860M Graphics (24394 MiB, 23175 MiB free)". An integrated GPU reports
    the shared system memory it may use, not a dedicated amount; llama.cpp does not tell the two apart.
    """
    best = (None, None)
    for m in re.finditer(r"^\s*\S+?\d+:\s*(.+?)\s*\((\d+)\s*MiB", text, re.M):
        size = int(m.group(2)) * 1024 * 1024
        if best[1] is None or size > best[1]:
            best = (m.group(1), size)
    return best


def gpu_info(llama=LLAMA):
    try:
        out = subprocess.run([llama, "--list-devices"], capture_output=True, timeout=30, cwd=os.path.dirname(llama))
        return parse_devices((out.stdout + out.stderr).decode("utf-8", "replace"))
    except Exception:
        return None, None


def recommend(ram_bytes, gpu_bytes):
    """The 35B when total RAM is at least 15 GB, else the 9B. gpu_bytes is shown on screen only: llama.cpp reports an
    integrated GPU's shared memory, not dedicated memory, so it says nothing about what the PC can run."""
    if (ram_bytes or 0) >= RECOMMEND_35B_RAM:
        return "35"
    return "9"


def free_disk_bytes(path):
    probe = path
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    return shutil.disk_usage(probe).free


def bytes_needed(entry, models_dir):
    """Bytes still to download for this model, counting what is already in place or in .partial files."""
    need = 0
    for f in model_files(entry):
        path = os.path.join(models_dir, f["file"])
        if os.path.isfile(path):
            continue
        have = os.path.getsize(path + ".partial") if os.path.isfile(path + ".partial") else 0
        need += max(f["size"] - have, 0)
    return need


def disk_problem(entry, models_dir, free, extra=0):
    """None when the model fits; else a message saying how much space is needed. `extra` is other first-run bytes (ffmpeg)."""
    need = bytes_needed(entry, models_dir)
    if need == 0:
        return None
    need += extra
    required = need + DISK_MARGIN
    if free >= required:
        return None
    return (f"Not enough free disk space for {entry['name']}: it needs {required / GB:.1f} GB free "
            f"({need / GB:.1f} GB download + {DISK_MARGIN / GB:.0f} GB spare) and {free / GB:.1f} GB is free. "
            "Free up space or choose the other model.")


def choose_model(models, ram, gpu_name, gpu_bytes, models_dir, input_fn=input, out=print, extra=0):
    free = free_disk_bytes(models_dir)
    rec = recommend(ram, gpu_bytes)
    keys = {m["key"]: m for m in models}
    out("\nManeframe needs a language model. This PC:")
    out(f"  RAM:   {ram / GB:.1f} GB" if ram else "  RAM:   unknown")
    out(f"  GPU:   {gpu_name}, {gpu_bytes / GB:.1f} GB (as reported by llama-server)" if gpu_bytes else "  GPU:   not detected")
    out(f"  Disk:  {free / GB:.1f} GB free in {models_dir}")
    out("")
    for m in models:
        out(f"  [{m['key']}] {m['name']}: download {download_size(m) / GB:.1f} GB: "
            f"model {m['model']['size'] / GB:.1f} GB + vision {m['mmproj']['size'] / GB:.1f} GB")
    out(f"\nRecommended for this PC: {rec} ({keys[rec]['name']}).")
    while True:
        try:
            answer = input_fn(f"Press Enter for {rec}, or type {' or '.join(keys)}: ").strip()
        except EOFError:
            raise Stop("No model was chosen (no keyboard input). Start Maneframe from a console window.")
        key = answer or rec
        if key not in keys:
            out(f"Please type {' or '.join(keys)}, or just press Enter.")
            continue
        problem = disk_problem(keys[key], models_dir, free, extra)
        if problem:
            out(problem)
            continue
        return keys[key]


# ---- config.json (the choice and which files were already checked) ----

def config_path(data):
    return os.path.join(data, "config.json")


def read_config(data):
    try:
        with open(config_path(data), encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(data, cfg):
    tmp = config_path(data) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, config_path(data))


def clear_choice(data):
    """Forget the chosen model so the chooser runs again; downloaded files and their checks are kept."""
    cfg = read_config(data)
    cfg.pop("model", None)
    write_config(data, cfg)


def is_verified(cfg, part, path):
    rec = (cfg.get("verified") or {}).get(part["file"])
    return (isinstance(rec, dict) and rec.get("sha256") == part["sha256"] and rec.get("size") == part["size"]
            and os.path.isfile(path) and os.path.getsize(path) == part["size"])


def mark_verified(data, part):
    cfg = read_config(data)
    cfg.setdefault("verified", {})[part["file"]] = {"size": part["size"], "sha256": part["sha256"]}
    write_config(data, cfg)


# ---- Download ----

def range_header(have):
    return f"bytes={have}-" if have > 0 else None


def sha256_file(path, label=None, out=print, chunk=8 * 1024 * 1024):
    h = hashlib.sha256()
    total = max(os.path.getsize(path), 1)
    done = 0
    last = 0.0
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
            done += len(block)
            if label and time.time() - last > 1:
                last = time.time()
                out(f"\r  Checking {label}: {done * 100 // total}%", end="", flush=True)
    if label:
        out("\r" + " " * 70 + "\r", end="", flush=True)
    return h.hexdigest()


def format_size(n):
    """Binary units labelled GB and MB, as Windows Explorer shows them: GB with one decimal from 1 GB up, else whole MB."""
    return f"{n / GB:.1f} GB" if n >= GB else f"{n / MB:,.0f} MB"


def is_slow(samples, window=SLOW_WINDOW, min_rate=SLOW_MIN_RATE):
    """samples: (seconds, total bytes) pairs of one connection, oldest first. True when the last `window` seconds
    averaged less than `min_rate` bytes/s. False until the samples cover a full window, so a short dip never triggers."""
    if not samples:
        return False
    t_end, b_end = samples[-1]
    older = [s for s in samples if s[0] <= t_end - window]
    if not older:
        return False
    t0, b0 = older[-1]
    return (b_end - b0) / (t_end - t0) < min_rate


def format_progress(done, total, rate, label=""):
    eta = (total - done) / rate if rate > 0 else None
    if eta is None:
        eta_text = "unknown"
    elif eta >= 3600:
        eta_text = f"{int(eta // 3600)}h {int(eta % 3600 // 60):02d}m"
    else:
        eta_text = f"{int(eta // 60)}m {int(eta % 60):02d}s"
    amount = f"{done / GB:.1f} / {total / GB:.1f} GB" if total >= GB else f"{done / MB:,.0f} / {total / MB:,.0f} MB"
    return f"  {label} {amount}  {done * 100 / total:.1f}%  {rate / MB:.1f} MB/s  ETA {eta_text}   "


def _fetch(url, partial, total, label, out, chunk=1024 * 1024, timeout=30, cut_slow=False, clock=time.time):
    """One attempt: continue `partial` with a Range request. Raises OSError when the transfer breaks.

    With cut_slow, a connection that averages under SLOW_MIN_RATE over SLOW_WINDOW seconds is closed; returns True then."""
    have = os.path.getsize(partial) if os.path.isfile(partial) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "Maneframe-launcher"})
    rng = range_header(have)
    if rng:
        req.add_header("Range", rng)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # the partial file is not a prefix of this file: start over
            os.remove(partial)
        raise
    with resp:
        if resp.status == 206 and have:
            mode = "ab"
        else:  # 200: the server ignored the Range header and sends the whole file
            mode, have = "wb", 0
        start = clock()
        got = 0
        last = 0.0
        samples = [(start, 0)]
        slow = False
        with open(partial, mode) as f:
            while True:
                block = resp.read(chunk)
                if not block:
                    break
                f.write(block)
                got += len(block)
                now = clock()
                samples.append((now, got))
                if now - last > 0.5:
                    last = now
                    out("\r" + format_progress(have + got, total, got / max(now - start, 1e-6), label), end="", flush=True)
                if cut_slow and is_slow(samples):
                    slow = True
                    break
                while len(samples) > 1 and samples[1][0] <= now - SLOW_WINDOW:  # keep one sample at or before the window start
                    samples.pop(0)
        out("\r" + format_progress(have + got, total, got / max(clock() - start, 1e-6), label), end="", flush=True)
    out("")
    return slow


def download_file(url, dest, size, sha256, label=None, retries=DOWNLOAD_RETRIES, backoff=2.0, sleep=time.sleep,
                  out=print, timeout=30, clock=time.time):
    """Downloads url to dest through dest + '.partial' (resumable), checks size and sha256, then renames.

    Network errors are retried `retries` times with growing waits, each retry resuming from the bytes already on disk.
    A connection that stays under SLOW_MIN_RATE is replaced by a new Range request, SLOW_RECONNECTS times per file (D16).
    A checksum mismatch deletes the file and stops. Ctrl+C leaves the .partial file for the next start.
    """
    label = label or os.path.basename(dest)
    partial = dest + ".partial"
    attempt = 0
    reconnects = 0
    reconnected = False
    while True:
        if os.path.isfile(partial) and os.path.getsize(partial) > size:
            os.remove(partial)
        have = os.path.getsize(partial) if os.path.isfile(partial) else 0
        if have == size:
            break
        try:
            if have and not reconnected:
                out(f"  Resuming {label} at {format_size(have)}")
            reconnected = False
            if _fetch(url, partial, size, label, out, timeout=timeout, cut_slow=reconnects < SLOW_RECONNECTS, clock=clock):
                reconnects += 1
                reconnected = True
                out(f"  Slow connection, reconnecting ({reconnects} of {SLOW_RECONNECTS})")
                continue
        except (OSError, http.client.HTTPException) as e:
            out("")
            if attempt >= retries:
                raise Stop(f"Download of {label} failed after {retries} retries: {e}\n"
                           "Check the internet connection and start Maneframe again; the download resumes where it stopped.")
            attempt += 1
            wait = backoff * 2 ** (attempt - 1)
            out(f"  Download interrupted ({e}); retry {attempt} of {retries} in {wait:.0f} s")
            sleep(wait)
            continue
        if os.path.getsize(partial) == size:
            break
        if attempt >= retries:  # the server closed the connection early every time
            raise Stop(f"Download of {label} ended early ({os.path.getsize(partial)} of {size} bytes). Start Maneframe again to resume.")
        attempt += 1
        sleep(backoff * 2 ** (attempt - 1))
    got = sha256_file(partial, label=f"{label} checksum", out=out)
    if got != sha256:
        os.remove(partial)
        raise Stop(f"{label} is corrupt: its checksum is {got} but {sha256} was expected. The file was deleted. "
                   "Start Maneframe to download it again; if this repeats, report it.")
    os.replace(partial, dest)


def ensure_model(entry, data, out=print, downloader=None):
    """Makes sure both files of the model are in <data>\\models, downloading and checking what is missing.

    Files already there (downloaded earlier or placed by hand) are hashed once and recorded in config.json.
    Returns the model and mmproj paths.
    """
    downloader = downloader or download_file
    models_dir = os.path.join(data, "models")
    os.makedirs(models_dir, exist_ok=True)
    cfg = read_config(data)
    for part in model_files(entry):
        path = os.path.join(models_dir, part["file"])
        if os.path.isfile(path):
            if is_verified(cfg, part, path):
                continue
            if os.path.getsize(path) != part["size"]:
                raise Stop(f"{path} has the wrong size ({os.path.getsize(path)} bytes, expected {part['size']}). "
                           "Delete it and start Maneframe again to download the right file.")
            out(f"Checking {part['file']} once (a few minutes for a large file)...")
            if sha256_file(path, label=part["file"], out=out) != part["sha256"]:
                raise Stop(f"{path} does not match the expected file (checksum differs). "
                           "Delete it and start Maneframe again to download the right file.")
            mark_verified(data, part)
            continue
        out(f"Downloading {part['file']} ({format_size(part['size'])}) from huggingface.co/{entry['repo']}")
        t0 = time.time()
        downloader(file_url(entry, part), path, part["size"], part["sha256"], label=part["file"], out=out)
        out(f"  {part['file']} downloaded and checked in {time.time() - t0:.0f} s")
        mark_verified(data, part)
    cfg = read_config(data)
    cfg["model"] = entry["id"]
    write_config(data, cfg)
    return [os.path.join(models_dir, p["file"]) for p in model_files(entry)]


def model_ready(entry, data):
    cfg = read_config(data)
    return all(is_verified(cfg, p, os.path.join(data, "models", p["file"])) for p in model_files(entry))


def select_model(data, change=False, input_fn=input, out=print, extra=0):
    """The model and mmproj paths to run with. Asks (chooser) only when there is no valid choice with all files present."""
    models = load_manifest()
    if change:
        clear_choice(data)
    cfg = read_config(data)
    entry = next((m for m in models if m["id"] == cfg.get("model")), None)
    if entry is None or not model_ready(entry, data):
        os.makedirs(os.path.join(data, "models"), exist_ok=True)
        gpu_name, gpu_bytes = gpu_info()
        entry = choose_model(models, total_ram_bytes(), gpu_name, gpu_bytes, os.path.join(data, "models"), input_fn, out, extra)
    return ensure_model(entry, data, out)


# ---- ffmpeg (not bundled, D18): the pinned imageio-ffmpeg wheel is downloaded from PyPI at first run ----

def load_ffmpeg_pin(path=FFMPEG_PIN):
    try:
        with open(path, encoding="utf-8") as f:
            pin = json.load(f)
        for k in ("version", "url", "exe"):
            if not isinstance(pin[k], str) or not pin[k]:
                raise ValueError(k)
        if not isinstance(pin["size"], int) or pin["size"] <= 0:
            raise ValueError("size")
        if not re.fullmatch(r"[0-9a-f]{64}", str(pin["sha256"])):
            raise ValueError("sha256")
        return pin
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Stop(f"ffmpeg.json is missing or unreadable ({e}). Unzip the whole package again.")


def ffmpeg_path(data, pin):
    return os.path.join(data, "tools", "ffmpeg", pin["exe"].rsplit("/", 1)[-1])


def ffmpeg_bytes_needed(data, pin):
    return 0 if os.path.isfile(ffmpeg_path(data, pin)) else pin["size"]


def ensure_ffmpeg(data, pin=None, out=print, downloader=None):
    """Path of ffmpeg.exe in <data>\tools\ffmpeg, downloading the pinned wheel and extracting only the exe when missing."""
    pin = pin or load_ffmpeg_pin()
    downloader = downloader or download_file
    exe = ffmpeg_path(data, pin)
    if os.path.isfile(exe):
        return exe
    folder = os.path.dirname(exe)
    os.makedirs(folder, exist_ok=True)
    free = free_disk_bytes(folder)
    if free < 2 * pin["size"]:
        raise Stop(f"Not enough free disk space for the video tool: {2 * pin['size'] / GB:.2f} GB needed in {folder}, "
                   f"{free / GB:.1f} GB is free.")
    wheel = os.path.join(folder, pin["url"].rsplit("/", 1)[-1])
    out(f"Downloading the video tool (ffmpeg, {format_size(pin['size'])}) from PyPI (imageio-ffmpeg {pin['version']})")
    t0 = time.time()
    downloader(pin["url"], wheel, pin["size"], pin["sha256"], label="ffmpeg", out=out)
    out(f"  ffmpeg downloaded and checked in {time.time() - t0:.0f} s")
    tmp = exe + ".tmp"
    try:
        with zipfile.ZipFile(wheel) as z, z.open(pin["exe"]) as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp, exe)
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        if os.path.isfile(tmp):
            os.remove(tmp)
        raise Stop(f"Could not extract ffmpeg from {wheel}: {e}")
    os.remove(wheel)
    return exe


def stop(proc):
    if proc.poll() is None:
        print(f"Stopping pid {proc.pid}")
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


def main():
    if not os.path.isfile(PYTHON) or not os.path.isfile(LLAMA):
        raise Stop("This folder is incomplete (python\\ or llama\\ is missing). Unzip the whole package again.")
    data = data_dir()
    for sub in ("models", "uploads", "labels", "logs"):
        os.makedirs(os.path.join(data, sub), exist_ok=True)
    pin = load_ffmpeg_pin()
    try:
        model, mmproj = select_model(data, change="--change-model" in sys.argv[1:], extra=ffmpeg_bytes_needed(data, pin))
        ffmpeg = ensure_ffmpeg(data, pin)
    except KeyboardInterrupt:
        raise Stop("Stopped. A partly downloaded file is kept; start Maneframe again to continue it.")

    llama_port = free_port(PREFERRED_LLAMA_PORT)
    backend_port = free_port(PREFERRED_BACKEND_PORT, avoid=(llama_port,))
    print(f"Data folder: {data}")
    print(f"Ports: llama-server {llama_port}, Maneframe {backend_port}")
    llama_root = f"http://127.0.0.1:{llama_port}"
    llama_base = f"{llama_root}/v1"
    logs = os.path.join(data, "logs")
    job = KillOnClose()
    started = []
    try:
        llama_log = open(os.path.join(logs, "llama-server.log"), "wb")
        llama = subprocess.Popen(
            [LLAMA, "-m", model, "--mmproj", mmproj, "-c", LLM_CTX, "-np", "1", "-ngl", "99",
             "--host", "127.0.0.1", "--port", str(llama_port), "--jinja"],
            cwd=os.path.dirname(LLAMA), stdout=llama_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        started.append(llama)
        job.add(llama)
        print("Loading the language model (a minute or more the first time)...")
        deadline = time.time() + LLAMA_READY_TIMEOUT
        while True:
            code, body = http_get(f"{llama_root}/health")
            if code == 200 and '"ok"' in body.replace(" ", ""):
                break
            if llama.poll() is not None:
                raise Stop(f"llama-server stopped (exit {llama.returncode}). See {os.path.join(logs, 'llama-server.log')}")
            if time.time() > deadline:
                raise Stop(f"The model did not load in {LLAMA_READY_TIMEOUT // 60} minutes. See {os.path.join(logs, 'llama-server.log')}")
            time.sleep(2)
        print("Language model ready.")
        print("Warming up the language model (the first start after a download can take a few minutes)...")
        warm = urllib.request.Request(
            f"{llama_base}/chat/completions", method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer local-key"},
            data=json.dumps({"model": "local", "max_tokens": 1, "temperature": 0,
                             "messages": [{"role": "user", "content": "Say hi."}],
                             "chat_template_kwargs": {"enable_thinking": False}}).encode())
        try:
            urllib.request.urlopen(warm, timeout=WARMUP_TIMEOUT).read()
        except Exception:  # best effort: the real run reports any actual model problem
            print("Warm-up did not finish; the first commentary may take longer.")

        env = dict(os.environ)
        env.update({
            "UPLOAD_DIR": os.path.join(data, "uploads"),
            "LABELS_DIR": os.path.join(data, "labels"),
            "LLM_BASE_URL": llama_base,
            "PUBLIC_BASE_URL": f"http://localhost:{backend_port}",
            "FFMPEG_PATH": ffmpeg,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        api_log = open(os.path.join(logs, "backend.log"), "wb")
        api = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(backend_port)],
            cwd=BACKEND_DIR, env=env, stdout=api_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        started.append(api)
        job.add(api)
        deadline = time.time() + BACKEND_READY_TIMEOUT
        while http_get(f"http://127.0.0.1:{backend_port}/api/styles")[0] != 200:
            if api.poll() is not None:
                raise Stop(f"The backend stopped (exit {api.returncode}). See {os.path.join(logs, 'backend.log')}")
            if time.time() > deadline:
                raise Stop(f"The backend did not start in {BACKEND_READY_TIMEOUT} seconds. See {os.path.join(logs, 'backend.log')}")
            time.sleep(1)
        url = f"http://localhost:{backend_port}"
        webbrowser.open(url)
        print(f"\nManeframe is running at {url}\nLeave this window open. Close it, or press Ctrl+C, to stop Maneframe.")
        while api.poll() is None and llama.poll() is None:
            time.sleep(1)
        raise Stop("A Maneframe process stopped unexpectedly. See the logs in " + logs)
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        for p in started:
            stop(p)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Stop as e:
        print(f"\n{e}")
        sys.exit(2)
