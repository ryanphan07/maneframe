import os
from typing import Literal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8")

    LLM_BASE_URL: str = "http://127.0.0.1:8080/v1"
    LLM_API_KEY: str = "local-key"
    LLM_MODEL: str = "local"  # llama-server serves whatever it loaded; the name is not used for routing
    LLM_TIMEOUT: float = 300.0  # seconds per call; a slow local model needs minutes per window
    LLM_ENABLE_THINKING: bool = False
    LLM_MAX_TOKENS: int = 500
    LLM_JSON_SCHEMA: bool = True  # send response_format with a JSON schema (constrained output)
    LLM_TEMPERATURE: float = 0.8
    # Used by start.ps1 only (the backend never launches the model): where llama-server lives and what it loads
    LLAMA_SERVER_PATH: str = ""  # empty: llama-server from PATH
    LLM_MODEL_PATH: str = ""
    LLM_MMPROJ_PATH: str = ""
    LLM_CTX: int = 8192  # llama-server -c; the prompts need about 3600 tokens per call

    WINDOW_SECONDS: float = 10.0
    FRAMES_PER_WINDOW: int = 6
    FRAME_WIDTH: int = 896
    MAX_QUIPS_PER_WINDOW: int = 3
    MIN_QUIP_GAP_SEC: float = 4.5  # measured TTS: about 4.5 s for an 8-word line

    TTS_ENGINE: Literal["edge", "piper"] = "edge"  # edge: online edge-tts (mp3); piper: offline CPU voice (wav), see README
    PIPER_MODEL_PATH: str = "models/piper/en_US-lessac-medium.onnx"  # relative paths start at the repo root; the folder is git-ignored
    PIPER_LENGTH_SCALE: float = 0.9  # below 1 speaks faster (edge's +10% is about 0.9)
    TTS_VOICE: str = "en-US-GuyNeural"
    TTS_RATE: str = "+10%"
    TTS_ENABLED: bool = True
    TTS_TIMEOUT_SECONDS: float = 30.0
    TTS_CONCURRENCY: int = 1  # edge-tts is an unofficial endpoint; parallel calls were seen to fail
    TTS_START_STAGGER_SECONDS: float = 0.3  # minimum gap between clip starts
    TTS_RETRIES: int = 1  # extra tries after a transient failure (timeout, dropped connection)
    TTS_RETRY_DELAY_SECONDS: float = 1.0
    MAX_SHIFT_SEC: float = 2.0

    FFMPEG_PATH: str = ""  # empty: use the ffmpeg binary bundled with imageio-ffmpeg
    EXPORT_TIMEOUT_SECONDS: float = 1800.0  # max time for one mp4 render
    TRANSCODE_TIMEOUT_SECONDS: float = 3600.0  # hard cap on converting an upload; progress keeps the job from going stale, see JOB_STALE_SECONDS

    LOG_LEVEL: str = "INFO"

    JOB_TTL_SECONDS: int = 3600
    JOB_STALE_SECONDS: int = 1800  # a running job with no progress update for this long is failed so uploads unblock; a conversion reports progress while ffmpeg advances, so only a hung one goes stale
    CLEANUP_INTERVAL_SECONDS: int = 600
    MAX_VIDEO_SECONDS: float = 1800.0  # guard against accidents; long clips are a goal (docs/DECISIONS.md D1)
    MAX_UPLOAD_MB: int = 2048
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]
    PUBLIC_BASE_URL: str = "http://localhost:8000"
    LABELS_DIR: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "labels"))  # kept after jobs expire
    UPLOAD_DIR: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uploads"))


settings = Settings()
