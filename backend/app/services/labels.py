"""
Moment labels for eval and future training data.
Stored per video content hash (not job id) so they outlive the job and its expiry.
"""

import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache

from app.config import settings
from app.services.llm import GLOSSARY_PATH

QUALITIES = ("good", "needs work", "none")
OTHER = "other"
MAX_NOTE_CHARS = 500
CSV_FIELDS = ("id", "t", "label", "quality", "note", "created_at")

_lock = threading.Lock()


class LabelError(ValueError):
    """Invalid label input; the message is safe to show to the client."""


def label_options() -> list[str]:
    """Terms from the glossary (the text before the colon, without the parenthetical alternate), plus 'other'."""
    terms = []
    for line in GLOSSARY_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("- ") and ":" in line:
            term = re.sub(r"\s*\(.*?\)", "", line[2:].split(":", 1)[0]).strip()
            if term:
                terms.append(term)
    return terms + [OTHER]


@lru_cache(maxsize=32)
def _hash_file(path: str, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def video_hash(path: str) -> str:
    """SHA-256 of the video file; cached per (path, size, mtime) since a 2 GB hash takes seconds."""
    st = os.stat(path)
    return _hash_file(path, st.st_size, st.st_mtime_ns)


def _file(digest: str) -> str:
    return os.path.join(settings.LABELS_DIR, f"{digest}.json")


def list_labels(digest: str) -> list[dict]:
    try:
        with open(_file(digest), encoding="utf-8") as f:
            labels = json.load(f)
    except FileNotFoundError:
        return []
    return sorted(labels, key=lambda l: (l["t"], l["created_at"]))


def _write(digest: str, labels: list[dict]) -> None:
    os.makedirs(settings.LABELS_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=settings.LABELS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(labels, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _file(digest))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def add_label(digest: str, duration: float, t: float, label: str, quality: str, note: str) -> dict:
    if not math.isfinite(t) or not 0 <= t <= duration:
        raise LabelError(f"Time must be between 0 and {duration:.2f} seconds.")
    if label not in label_options():
        raise LabelError("Unknown label. Choose one of the listed terms or 'other'.")
    if quality not in QUALITIES:
        raise LabelError(f"Quality must be one of: {', '.join(QUALITIES)}.")
    note = (note or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        raise LabelError(f"Note is too long (max {MAX_NOTE_CHARS} characters).")
    entry = {
        "id": uuid.uuid4().hex[:8],
        "t": round(float(t), 2),
        "label": label,
        "quality": quality,
        "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with _lock:
        labels = list_labels(digest)
        labels.append(entry)
        _write(digest, labels)
    return entry


def delete_label(digest: str, label_id: str) -> bool:
    with _lock:
        labels = list_labels(digest)
        kept = [l for l in labels if l["id"] != label_id]
        if len(kept) == len(labels):
            return False
        _write(digest, kept)
    return True


def _csv_safe(value: str) -> str:
    """Stop spreadsheet apps from running a note that starts like a formula."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def to_csv(labels: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for l in labels:
        writer.writerow({**l, "note": _csv_safe(l["note"])})
    return out.getvalue()
