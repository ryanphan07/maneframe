"""Starts Maneframe from the portable package: llama-server, then the backend, then the browser.

Runs with the package's own Python (python\\python.exe), standard library only. Layout next to this file:
python\\ (private Python), backend\\app, frontend\\dist, llama\\llama-server.exe. Data lives outside the package
(MANEFRAME_DATA_DIR, else %LOCALAPPDATA%\\Maneframe). Ctrl+C, closing the window or a crash stops both servers.
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from ctypes import wintypes

ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(ROOT, "python", "python.exe")
LLAMA = os.path.join(ROOT, "llama", "llama-server.exe")
BACKEND_DIR = os.path.join(ROOT, "backend")

# Until the first-run model chooser exists the model is placed by hand in <data dir>\models.
MODEL_FILE = "Qwen3.6-35B-A3B-UD-Q3_K_XL.gguf"
MMPROJ_FILE = "mmproj-Qwen3.6-35B-F16.gguf"
LLM_CTX = "8192"  # the prompts need about 3600 tokens per call
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
    model = os.path.join(data, "models", MODEL_FILE)
    mmproj = os.path.join(data, "models", MMPROJ_FILE)
    missing = [p for p in (model, mmproj) if not os.path.isfile(p)]
    if missing:
        raise Stop("Maneframe needs its language model, which is not there yet.\n"
                   f"Put these two files in this folder:\n  {os.path.join(data, 'models')}\n"
                   f"  {MODEL_FILE}\n  {MMPROJ_FILE}\n"
                   "(Missing: " + ", ".join(os.path.basename(p) for p in missing) + ")\n"
                   "Then start Maneframe again.")

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
        warm = urllib.request.Request(
            f"{llama_base}/chat/completions", method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer local-key"},
            data=json.dumps({"model": "local", "max_tokens": 1, "temperature": 0,
                             "messages": [{"role": "user", "content": "Say hi."}],
                             "chat_template_kwargs": {"enable_thinking": False}}).encode())
        try:
            urllib.request.urlopen(warm, timeout=120).read()
        except Exception as e:  # best effort: the real run reports any actual model problem
            print(f"Warm-up request failed (continuing): {e}")

        env = dict(os.environ)
        env.update({
            "UPLOAD_DIR": os.path.join(data, "uploads"),
            "LABELS_DIR": os.path.join(data, "labels"),
            "LLM_BASE_URL": llama_base,
            "PUBLIC_BASE_URL": f"http://localhost:{backend_port}",
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
