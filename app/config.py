"""Argus / photometa configuration — local-first, env driven.

Phase 2: service-ized, queueable, Tailscale-friendly.
"""

import os
import logging
from pathlib import Path

# Data dir for this service (db, tmp, exports, sidecars)
DATA_DIR = Path(os.environ.get("ARGUS_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "argus.db"

# Ollama (mock by default for safety)
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
VISION_MODEL = os.environ.get("ARGUS_VISION_MODEL", "qwen3-vl:32b")

# Basic server
HOST = os.environ.get("ARGUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARGUS_PORT", "8010"))  # avoid clashing with mise 8400 / odysseus 7010

# Photo handling
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}

# Prompt / analysis tuning
DEFAULT_MAX_TAGS = int(os.environ.get("ARGUS_MAX_TAGS", "12"))

# Backend for analysis: "real" (Ollama vision) or "mock" (synthetic data, no heavy models)
# IMPORTANT: Default and recommended for Phase 2 dev = "mock". Never change to "real" on mickey without explicit approval.
VISION_BACKEND = os.environ.get("ARGUS_VISION_BACKEND", "mock").lower()  # "mock" or "real"

# Phase 2 service settings
SERVICE_MODE = os.environ.get("ARGUS_SERVICE_MODE", "standalone").lower()  # standalone | odysseus-style
QUEUE_ENABLED = os.environ.get("ARGUS_QUEUE_ENABLED", "true").lower() == "true"
MAX_CONCURRENT_JOBS = int(os.environ.get("ARGUS_MAX_CONCURRENT_JOBS", "2"))
CLOUD_BACKEND = os.environ.get("ARGUS_CLOUD_BACKEND", "disabled").lower()  # disabled | stub | simulated (mock only)
COST_TRACKING = os.environ.get("ARGUS_COST_TRACKING", "true").lower() == "true"
CLOUD_COST_PER_IMAGE = float(os.environ.get("ARGUS_CLOUD_COST_PER_IMAGE", "0.00123"))
TAILSCALE_HINT = os.environ.get("ARGUS_TAILSCALE_HINT", "mickey")  # e.g. "mickey" or full tailscale name

# Phase 4: optional bearer auth (disabled when unset — local dev default).
API_TOKEN = os.environ.get("ARGUS_API_TOKEN") or None

# Phase 3 slice 2: direct import from mise galleries.
# Set ARGUS_MISE_MEDIA_ROOT to the mise DATA_DIR/media (or equivalent) so that
# --mise-gallery-id / mise_gallery_id= can auto-resolve to .../<id>/original
# using mise's storage layout (MEDIA_DIR / gallery_id / "original" / stored).
# If unset, caller must pass explicit folder path to the originals.
MISE_MEDIA_ROOT = Path(os.environ.get("ARGUS_MISE_MEDIA_ROOT", "")) if os.environ.get("ARGUS_MISE_MEDIA_ROOT") else None

# Logging
LOG_LEVEL = os.environ.get("ARGUS_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("argus")
