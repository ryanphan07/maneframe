import pytest


@pytest.fixture(autouse=True)
def clean_job_store():
    """The module-level job_store is shared; unfinished jobs from one test must not block uploads in the next."""
    from app.services.jobs import job_store

    job_store._jobs.clear()
    job_store._subscribers.clear()
    yield
    job_store._jobs.clear()
    job_store._subscribers.clear()


@pytest.fixture(autouse=True)
def isolated_data_dirs(tmp_path_factory, monkeypatch):
    """Every test gets its own UPLOAD/LABELS dirs (export dirs live under UPLOAD_DIR). Starting the app runs sweep_uploads,
    which deletes files in UPLOAD_DIR, so a test must never see the real one. Tests that need a specific dir override this."""
    from app import main as main_module
    from app.config import settings

    root = tmp_path_factory.mktemp("data")
    uploads = root / "uploads"
    (uploads / "videos").mkdir(parents=True)
    (uploads / "audio").mkdir()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(settings, "LABELS_DIR", str(root / "labels"))
    monkeypatch.setattr(main_module, "VIDEO_DIR", str(uploads / "videos"))
    monkeypatch.setattr(main_module, "AUDIO_DIR", str(uploads / "audio"))
    yield
