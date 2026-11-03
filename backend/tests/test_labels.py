import csv
import io

import pytest

from app.config import settings
from app.services import labels as labels_module
from app.services.jobs import job_store
from test_api import client, fake_ai, upload, upload_dir, video_bytes  # noqa: F401  (fixtures)


@pytest.fixture
def job(client, upload_dir, fake_ai, video_bytes, monkeypatch):
    monkeypatch.setattr(settings, "LABELS_DIR", str(upload_dir / "labels"))
    return upload(client, video_bytes).json()["job_id"]


def body(**kw):
    return {"t": 1.0, "label": "Cai qing", "quality": "good", "note": "nice", **kw}


def test_label_options_come_from_the_glossary():
    opts = labels_module.label_options()
    assert "Cai qing" in opts and "Jong" in opts and opts[-1] == "other"
    assert not any("(" in o for o in opts)


def test_add_list_delete_roundtrip(client, job):
    r = client.post(f"/api/jobs/{job}/labels", json=body(t=2.0))
    assert r.status_code == 201
    first = r.json()
    client.post(f"/api/jobs/{job}/labels", json=body(t=0.5, label="other", quality="none", note=""))
    listing = client.get(f"/api/jobs/{job}/labels").json()
    assert [l["t"] for l in listing["labels"]] == [0.5, 2.0]
    assert "other" in listing["label_options"] and listing["qualities"] == ["good", "needs work", "none"]
    assert client.delete(f"/api/jobs/{job}/labels/{first['id']}").status_code == 204
    assert [l["t"] for l in client.get(f"/api/jobs/{job}/labels").json()["labels"]] == [0.5]
    assert client.delete(f"/api/jobs/{job}/labels/{first['id']}").status_code == 404


@pytest.mark.parametrize("bad", [
    {"t": -0.1}, {"t": 999}, {"label": "Not a term"}, {"quality": "great"}, {"note": "x" * 501},
])
def test_invalid_input_is_rejected(client, job, bad):
    assert client.post(f"/api/jobs/{job}/labels", json=body(**bad)).status_code == 400
    assert client.get(f"/api/jobs/{job}/labels").json()["labels"] == []


def test_nan_and_missing_fields_are_rejected(client, job):
    nan = client.post(f"/api/jobs/{job}/labels", content='{"t": NaN, "label": "other", "quality": "good"}',
                      headers={"content-type": "application/json"})
    assert nan.status_code in (400, 422)
    assert client.post(f"/api/jobs/{job}/labels", json={"t": 1}).status_code == 422


def test_unknown_job_404(client, upload_dir, monkeypatch):
    monkeypatch.setattr(settings, "LABELS_DIR", str(upload_dir / "labels"))
    assert client.get("/api/jobs/nope/labels").status_code == 404
    assert client.post("/api/jobs/nope/labels", json=body()).status_code == 404
    assert client.delete("/api/jobs/nope/labels/x").status_code == 404
    assert client.get("/api/jobs/nope/labels.csv").status_code == 404


def test_labels_survive_job_expiry_and_follow_the_video_hash(client, job, video_bytes, upload_dir):
    client.post(f"/api/jobs/{job}/labels", json=body())
    stored = list((upload_dir / "labels").glob("*.json"))
    assert len(stored) == 1
    job_store._jobs.clear()
    assert client.get(f"/api/jobs/{job}/labels").status_code == 404
    assert [l["label"] for l in labels_module.list_labels(stored[0].stem)] == ["Cai qing"]
    again = upload(client, video_bytes).json()["job_id"]  # the same clip under a new job id sees the same labels
    assert len(client.get(f"/api/jobs/{again}/labels").json()["labels"]) == 1


def test_csv_export_and_formula_guard(client, job):
    client.post(f"/api/jobs/{job}/labels", json=body(note='=HYPERLINK("x"), "q"'))
    r = client.get(f"/api/jobs/{job}/labels.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0]["label"] == "Cai qing" and rows[0]["t"] == "1.0"
    assert rows[0]["note"].startswith("'=")
    assert list(rows[0]) == ["id", "t", "label", "quality", "note", "created_at"]


def test_label_file_records_filename_and_duration(client, job, upload_dir):
    client.post(f"/api/jobs/{job}/labels", json=body())
    import json

    stored = json.loads(next((upload_dir / "labels").glob("*.json")).read_text(encoding="utf-8"))
    assert stored["filename"] == "clip.avi"
    assert stored["duration"] == pytest.approx(3.0, abs=0.3)
    assert len(stored["labels"]) == 1


def test_label_routes_do_not_probe_the_video_again(client, job, monkeypatch):
    from app.services import video_processor as vp

    def no_probe(*a, **k):
        raise AssertionError("duration must come from the job, not a new probe")

    monkeypatch.setattr(vp, "get_video_duration", no_probe)
    assert client.post(f"/api/jobs/{job}/labels", json=body()).status_code == 201
    assert client.get(f"/api/jobs/{job}/labels").status_code == 200
    assert client.get(f"/api/jobs/{job}/labels.csv").status_code == 200


def test_labels_before_the_video_is_prepared_are_409(client, upload_dir, monkeypatch):
    monkeypatch.setattr(settings, "LABELS_DIR", str(upload_dir / "labels"))
    job_store.create("x.avi", job_id="early")
    assert client.get("/api/jobs/early/labels").status_code == 409


def test_legacy_list_files_still_load_and_are_upgraded_on_write(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(settings, "LABELS_DIR", str(tmp_path))
    legacy = [{"id": "a1", "t": 1.0, "label": "Jong", "quality": "good", "note": "", "created_at": "2026-01-01T00:00:00+00:00"}]
    (tmp_path / "abc.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert labels_module.list_labels("abc") == legacy
    assert labels_module.load("abc")["filename"] is None
    labels_module.add_label("abc", 10.0, 2.0, "other", "none", "", "old.mp4")
    data = json.loads((tmp_path / "abc.json").read_text(encoding="utf-8"))
    assert data["filename"] == "old.mp4" and data["duration"] == 10.0 and len(data["labels"]) == 2
    assert labels_module.delete_label("abc", "a1")
    assert json.loads((tmp_path / "abc.json").read_text(encoding="utf-8"))["filename"] == "old.mp4"


def test_all_labels_csv_needs_no_live_job(client, upload_dir, monkeypatch):
    monkeypatch.setattr(settings, "LABELS_DIR", str(upload_dir / "labels"))
    empty = client.get("/api/labels.csv")
    assert empty.status_code == 200 and empty.text.strip().split(",") == ["hash", "filename", "id", "t", "label", "quality", "note", "created_at"]

    labels_module.add_label("h1", 5.0, 1.0, "Cai qing", "good", "first", "a.mp4")
    labels_module.add_label("h1", 5.0, 2.0, "other", "none", "", "ignored_later_name.mp4")
    labels_module.add_label("h2", 5.0, 0.5, "Jong", "needs work", '=cmd()', "=evil.mp4")
    (upload_dir / "labels" / "stray.json").write_text("{not json", encoding="utf-8")
    (upload_dir / "labels" / "notes.txt").write_text("x", encoding="utf-8")

    r = client.get("/api/labels.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [(x["hash"], x["filename"], x["label"]) for x in rows] == [
        ("h1", "a.mp4", "Cai qing"), ("h1", "a.mp4", "other"), ("h2", "'=evil.mp4", "Jong"),
    ]
    assert rows[2]["note"] == "'=cmd()"
