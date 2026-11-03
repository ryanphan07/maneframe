from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app as real_app, mount_frontend


def _app_with_routes() -> FastAPI:
    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    return app


def test_serves_built_frontend_without_shadowing_routes(tmp_path):
    (tmp_path / "index.html").write_text("<html>maneframe</html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    app = _app_with_routes()
    assert mount_frontend(app, str(tmp_path)) is True
    client = TestClient(app)
    assert "maneframe" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/api/ping").json() == {"ok": True}
    assert client.get("/api/nope").status_code == 404


def test_no_mount_without_a_build(tmp_path):
    app = _app_with_routes()
    assert mount_frontend(app, str(tmp_path / "missing")) is False
    assert mount_frontend(app, str(tmp_path)) is False  # folder without index.html
    client = TestClient(app)
    assert client.get("/").status_code == 404
    assert client.get("/api/ping").status_code == 200


def test_real_app_registers_frontend_mount_last():
    paths = [getattr(r, "path", None) for r in real_app.routes]
    assert "/ws/analyze/{job_id}" in paths and "/api/styles" in paths
    mounts = [i for i, r in enumerate(real_app.routes) if getattr(r, "name", "") == "frontend"]
    if mounts:  # present only when frontend/dist is built
        assert mounts == [len(real_app.routes) - 1]
