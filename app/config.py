"""Argus / photometa configuration — local-first, env driven."""

import os
from pathlib import Path

# Data dir for this service (db, tmp, exports)
DATA_DIR = Path(os.environ.get("ARGUS_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "argus.db"

# Ollama
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
VISION_MODEL = os.environ.get("ARGUS_VISION_MODEL", "qwen3-vl:32b")

# Basic server
HOST = os.environ.get("ARGUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARGUS_PORT", "8010"))  # avoid clashing with mise 8400 / odysseus 7010

# Photo handling
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}

# Prompt / analysis tuning (start conservative)
DEFAULT_MAX_TAGS = int(os.environ.get("ARGUS_MAX_TAGS", "12"))
