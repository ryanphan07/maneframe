import os

from app import main as main_module
from app.config import settings
from app.services.export import export_dir

REAL_UPLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uploads"))
REAL_LABELS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "labels"))


def _inside(path: str, root: str) -> bool:
    path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root + os.sep)


def test_tests_never_resolve_to_real_data_dirs():
    from app.services.labels import _file

    for p in (_file("0" * 64), settings.UPLOAD_DIR, main_module.VIDEO_DIR, main_module.AUDIO_DIR, export_dir("a" * 32), settings.LABELS_DIR):
        assert not _inside(p, REAL_UPLOAD_DIR), p
        assert not _inside(p, REAL_LABELS_DIR), p
