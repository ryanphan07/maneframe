import asyncio
import contextlib
import logging
import os
import shutil
import uuid

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.services.export import ExportError, render_export
from app.services import llm as llm_service
from app.services.jobs import TERMINAL, JobNotFoundError, job_store
from app.services.pipeline import cleanup_job_files, purge_expired_jobs, run_pipeline, sweep_uploads
from app.services.video_processor import validate_upload_metadata, sanitize_filename, is_video_corrupt, get_video_duration

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("maneframe.main")
BUSY_DETAIL = "Another clip is still being processed. Please wait for it to finish."


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(settings.CLEANUP_INTERVAL_SECONDS)
        try:
            purge_expired_jobs()
        except Exception:
            logger.exception("Periodic cleanup failed")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    llm_service.load_system_prompt()  # fail at startup, not on the first window, if a prompt file is missing
    await asyncio.to_thread(sweep_uploads)
    task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
VIDEO_DIR = os.path.join(settings.UPLOAD_DIR, "videos")
AUDIO_DIR = os.path.join(settings.UPLOAD_DIR, "audio")
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
# Only audio is served; videos are never exposed.
app.mount("/uploads/audio", StaticFiles(directory=AUDIO_DIR), name="audio")


@app.exception_handler(Exception)
async def global_exception_handler(_, exc: Exception):
    logger.error("Unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.post("/api/upload")
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), style: str = Form(llm_service.DEFAULT_STYLE)):
    styles = llm_service.list_styles()
    if style not in styles:
        raise HTTPException(status_code=400, detail=f"Unknown style. Choose one of: {', '.join(styles)}.")
    purge_expired_jobs()  # kept videos pile up otherwise: job_store.create would drop expired jobs without deleting files
    if job_store.has_active():
        raise HTTPException(status_code=409, detail=BUSY_DETAIL)
    if not file.filename:
        raise HTTPException(status_code=400, detail="A filename is required.")
    safe_name = sanitize_filename(file.filename)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    file_size = file.size
    if file_size is None:
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(0)
    try:
        validate_upload_metadata(safe_name, file_size)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_id = uuid.uuid4().hex
    video_path = os.path.join(VIDEO_DIR, f"{job_id}_{safe_name}")
    try:
        def _save() -> None:
            with open(video_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

        await asyncio.to_thread(_save)
        if await asyncio.to_thread(is_video_corrupt, video_path):
            raise HTTPException(status_code=400, detail="The uploaded video file is corrupt.")
        try:
            duration = await asyncio.to_thread(get_video_duration, video_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if duration > settings.MAX_VIDEO_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"Video is too long. Maximum length is {settings.MAX_VIDEO_SECONDS:g} seconds.",
            )
        if job_store.has_active():  # another upload finished saving first
            raise HTTPException(status_code=409, detail=BUSY_DETAIL)
        job_store.create(video_path, job_id=job_id, style=style)
    except BaseException:
        cleanup_job_files(job_id, video_path)
        raise

    background_tasks.add_task(run_pipeline, job_id, video_path, style)
    return {"job_id": job_id, "filename": safe_name}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    try:
        job = job_store.get(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "result": job.result.model_dump() if job.result else None,
    }


@app.get("/api/jobs/{job_id}/export.mp4")
async def export_job(job_id: str, captions: bool = True):
    """Render the finished job as an mp4 (original audio ducked under the commentary, optional burned-in captions)."""
    try:
        job = job_store.get(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed" or job.result is None:
        raise HTTPException(status_code=409, detail="The commentary is not ready yet.")
    quips = [q.model_dump() for q in job.result.quips]
    try:
        path = await render_export(job_id, job.video_path, quips, captions)
    except ExportError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    original = os.path.basename(job.video_path).removeprefix(f"{job_id}_")
    return FileResponse(path, media_type="video/mp4", filename=f"maneframe_{os.path.splitext(original)[0]}.mp4")


@app.websocket("/ws/analyze/{job_id}")
async def websocket_endpoint(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        job_store.get(job_id)
    except JobNotFoundError:
        await websocket.send_json({"status": "error", "progress": 0, "message": "Job not found"})
        await websocket.close(code=4404)
        return

    queue = job_store.subscribe(job_id)
    recv = asyncio.ensure_future(websocket.receive_text())
    get = None
    try:
        while True:
            if get is None:
                get = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({get, recv}, return_when=asyncio.FIRST_COMPLETED)
            if get in done:
                snap = get.result()
                get = None
                await websocket.send_json(snap)
                if snap["status"] in TERMINAL:
                    await websocket.close(code=1000)
                    return
            if recv in done:
                recv.result()  # raises WebSocketDisconnect when the client leaves
                recv = asyncio.ensure_future(websocket.receive_text())  # ignore client messages
    except WebSocketDisconnect:
        pass
    finally:
        for task in (get, recv):
            if task is not None and not task.done():
                task.cancel()
        job_store.unsubscribe(job_id, queue)
