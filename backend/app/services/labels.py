"""
Moment labels for eval and future training data.
Stored per video content hash (not job id) so they outlive the job and its expiry. Each file is
{"filename", "duration", "labels"}; older files were a bare list of labels.
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
MAX_TERM_CHARS = 40
CSV_FIELDS = ("id", "t", "label", "quality", "note", "created_at")
ALL_CSV_FIELDS = ("hash", "filename") + CSV_FIELDS

_lock = threading.Lock()


class LabelError(ValueError):
    """Invalid label input; the message is safe to show to the client."""


def glossary_terms(text: str) -> list[str]:
    """Glossary terms: bullets written as "Term: description" (parenthetical alternates dropped), at any depth.
    A bullet with nothing after its colon ("- Stacks:") is a group heading: its children are terms. A bullet that has
    a description owns the lines under it as steps ("- 7-star: ..." then "    - snap left ..."), and those are not terms.
    Section headers and wrapped continuation lines are not bullets, so they never match."""
    terms: list[str] = []
    stack: list[tuple[int, bool]] = []  # (indent, is_group_heading) of the open ancestors
    for line in text.splitlines():
        m = re.match(r"^(\s*)- (.*)", line)
        if not m:
            continue
        indent, body = len(m.group(1).expandtabs(4)), m.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        head, colon, desc = body.partition(":")
        is_group = bool(colon) and not desc.strip()
        term = re.sub(r"\s*\(.*?\)", "", head).strip()
        if colon and not is_group and term and len(term) <= MAX_TERM_CHARS and all(g for _, g in stack) and term not in terms:
            terms.append(term)
        stack.append((indent, is_group))
    return terms


def label_options() -> list[str]:
    """The glossary's terms (see glossary_terms), plus 'other'."""
    return glossary_terms(GLOSSARY_PATH.read_text(encoding="utf-8")) + [OTHER]


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


def load(digest: str) -> dict:
    """The video's label file as {"filename", "duration", "labels"} (labels sorted by time)."""
    try:
        with open(_file(digest), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    if isinstance(data, list):  # the original format
        data = {"labels": data}
    labels = sorted(data.get("labels", []), key=lambda l: (l["t"], l["created_at"]))
    return {"filename": data.get("filename"), "duration": data.get("duration"), "labels": labels}


def list_labels(digest: str) -> list[dict]:
    return load(digest)["labels"]


def list_all() -> list[dict]:
    """Every labeled video, whether or not its job still exists: [{"hash", "filename", "duration", "labels"}]."""
    entries = []
    try:
        names = sorted(os.listdir(settings.LABELS_DIR))
    except FileNotFoundError:
        return entries
    for name in names:
        digest, ext = os.path.splitext(name)
        if ext != ".json":
            continue
        try:
            data = load(digest)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue  # not one of our files
        if data["labels"]:
            entries.append({"hash": digest, **data})
    return entries


def _write(digest: str, data: dict) -> None:
    os.makedirs(settings.LABELS_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=settings.LABELS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _file(digest))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def add_label(digest: str, duration: float, t: float, label: str, quality: str, note: str, filename: str = "") -> dict:
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
        data = load(digest)
        data["labels"].append(entry)
        data["filename"] = data["filename"] or filename or None  # the first name this video was labeled under stays
        data["duration"] = round(duration, 3)
        _write(digest, data)
    return entry


def delete_label(digest: str, label_id: str) -> bool:
    with _lock:
        data = load(digest)
        kept = [l for l in data["labels"] if l["id"] != label_id]
        if len(kept) == len(data["labels"]):
            return False
        _write(digest, {**data, "labels": kept})
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


def all_to_csv(entries: list[dict]) -> str:
    """One row per label across videos: hash and original filename first, then the label fields."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=ALL_CSV_FIELDS)
    writer.writeheader()
    for entry in entries:
        for l in entry["labels"]:
            writer.writerow({**l, "hash": entry["hash"], "filename": _csv_safe(entry["filename"] or ""), "note": _csv_safe(l["note"])})
    return out.getvalue()
