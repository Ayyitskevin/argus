"""Argus / photometa configuration — local-first, env driven.

Phase 2: service-ized, queueable, Tailscale-friendly.
"""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=False)

# Data dir for this service (db, tmp, exports, sidecars)
DATA_DIR = Path(os.environ.get("ARGUS_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "argus.db"

# Vision: Grok/xAI API (mock by default for CI safety)
XAI_API_KEY = os.environ.get("XAI_API_KEY") or None
XAI_API_BASE = os.environ.get("XAI_API_BASE", "https://api.x.ai/v1")
XAI_TIMEOUT = float(os.environ.get("XAI_TIMEOUT", "180"))
VISION_MODEL = os.environ.get("ARGUS_VISION_MODEL", "grok-4-fast")

# Basic server
HOST = os.environ.get("ARGUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARGUS_PORT", "8010"))  # avoid clashing with mise 8400 / odysseus 7010

# Photo handling
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}

# Prompt / analysis tuning
DEFAULT_MAX_TAGS = int(os.environ.get("ARGUS_MAX_TAGS", "12"))

# Backend: "mock" (CI/dev default) or "grok"/"real" (xAI Grok vision — requires XAI_API_KEY)
_raw_backend = os.environ.get("ARGUS_VISION_BACKEND", "mock").lower()
VISION_BACKEND = "grok" if _raw_backend == "real" else _raw_backend  # "real" alias for grok

# Phase 2 service settings
SERVICE_MODE = os.environ.get("ARGUS_SERVICE_MODE", "standalone").lower()  # standalone | odysseus-style
QUEUE_ENABLED = os.environ.get("ARGUS_QUEUE_ENABLED", "true").lower() == "true"
MAX_CONCURRENT_JOBS = int(os.environ.get("ARGUS_MAX_CONCURRENT_JOBS", "2"))
MAX_QUEUE_DEPTH = int(os.environ.get("ARGUS_MAX_QUEUE_DEPTH", "100"))
JOB_MAX_RETRIES = int(os.environ.get("ARGUS_JOB_MAX_RETRIES", "1"))
JOB_RETENTION_DAYS = int(os.environ.get("ARGUS_JOB_RETENTION_DAYS", "90"))
CLOUD_BACKEND = os.environ.get("ARGUS_CLOUD_BACKEND", "disabled").lower()  # disabled | stub | simulated | real
COST_TRACKING = os.environ.get("ARGUS_COST_TRACKING", "true").lower() == "true"
CLOUD_COST_PER_IMAGE = float(os.environ.get("ARGUS_CLOUD_COST_PER_IMAGE", "0.00123"))
TAILSCALE_HINT = os.environ.get("ARGUS_TAILSCALE_HINT", "mickey")  # e.g. "mickey" or full tailscale name

# Phase 4: optional bearer auth (disabled when unset — local dev default).
API_TOKEN = os.environ.get("ARGUS_API_TOKEN") or None

# Phase 10 — SaaS / multi-tenant cloud vision (off by default on homelab)
SAAS_MODE = os.environ.get("ARGUS_SAAS_MODE", "false").lower() == "true"
CLOUD_COST_CAP_USD = float(os.environ.get("ARGUS_CLOUD_COST_CAP_USD", "0"))  # 0 = unlimited global
CLOUD_MONTHLY_IMAGE_CAP = int(os.environ.get("ARGUS_CLOUD_MONTHLY_IMAGE_CAP", "0"))  # 0 = unlimited global
TENANT_KEY_PEPPER = os.environ.get("ARGUS_TENANT_KEY_PEPPER") or API_TOKEN or "argus-dev-pepper"
DEFAULT_VISION_PROVIDER = os.environ.get("ARGUS_DEFAULT_VISION_PROVIDER", "grok").lower()

# Optional alternate cloud vision providers (SaaS deploy only — not homelab default)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or None
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or None
ANTHROPIC_VISION_MODEL = os.environ.get("ANTHROPIC_VISION_MODEL", "claude-sonnet-4-20250514")

# Phase 9: optional Prometheus text exposition
PROMETHEUS_ENABLED = os.environ.get("ARGUS_PROMETHEUS_ENABLED", "false").lower() == "true"

# Phase 11 — production SaaS (storage, rate limits, audit, billing)
STORAGE_BACKEND = os.environ.get("ARGUS_STORAGE_BACKEND", "local").lower()  # local | s3
S3_BUCKET = os.environ.get("ARGUS_S3_BUCKET") or None
S3_REGION = os.environ.get("ARGUS_S3_REGION", "us-east-1")
S3_ENDPOINT = os.environ.get("ARGUS_S3_ENDPOINT") or None  # MinIO / R2 custom endpoint
S3_ACCESS_KEY = os.environ.get("ARGUS_S3_ACCESS_KEY") or None
S3_SECRET_KEY = os.environ.get("ARGUS_S3_SECRET_KEY") or None
S3_PREFIX = os.environ.get("ARGUS_S3_PREFIX", "argus/tenants")

RATE_LIMIT_ENABLED = os.environ.get("ARGUS_RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_PER_MINUTE = int(os.environ.get("ARGUS_RATE_LIMIT_PER_MINUTE", "60"))
RATE_LIMIT_ANALYZE_PER_MINUTE = int(os.environ.get("ARGUS_RATE_LIMIT_ANALYZE_PER_MINUTE", "20"))
REDIS_URL = os.environ.get("ARGUS_REDIS_URL") or None

CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("ARGUS_CORS_ORIGINS", "").split(",")
    if origin.strip()
]

CAP_WARNING_THRESHOLD = float(os.environ.get("ARGUS_CAP_WARNING_THRESHOLD", "0.8"))
CAP_WEBHOOK_URL = os.environ.get("ARGUS_CAP_WEBHOOK_URL") or None
CAP_ALERT_EMAIL = os.environ.get("ARGUS_CAP_ALERT_EMAIL") or None
SMTP_HOST = os.environ.get("ARGUS_SMTP_HOST") or None
SMTP_PORT = int(os.environ.get("ARGUS_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("ARGUS_SMTP_USER") or None
SMTP_PASSWORD = os.environ.get("ARGUS_SMTP_PASSWORD") or None
SMTP_FROM = os.environ.get("ARGUS_SMTP_FROM") or SMTP_USER
STRUCTURED_LOGS = os.environ.get("ARGUS_STRUCTURED_LOGS", "true").lower() == "true"

AUDIT_LOG_ENABLED = os.environ.get("ARGUS_AUDIT_LOG_ENABLED", "true").lower() == "true"
AUDIT_LOG_RETENTION_DAYS = int(os.environ.get("ARGUS_AUDIT_LOG_RETENTION_DAYS", "90"))

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY") or None
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET") or None
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID") or None  # subscription price
SAAS_PUBLIC_URL = os.environ.get("ARGUS_SAAS_PUBLIC_URL", f"http://{HOST}:{PORT}")
STRIPE_SUCCESS_URL = os.environ.get(
    "STRIPE_SUCCESS_URL",
    f"{SAAS_PUBLIC_URL.rstrip('/')}/ui/saas/billing?success=1",
)
STRIPE_CANCEL_URL = os.environ.get(
    "STRIPE_CANCEL_URL",
    f"{SAAS_PUBLIC_URL.rstrip('/')}/ui/saas/billing?cancelled=1",
)
STRIPE_BILLING_PORTAL_RETURN_URL = os.environ.get(
    "STRIPE_BILLING_PORTAL_RETURN_URL",
    f"{SAAS_PUBLIC_URL.rstrip('/')}/ui/saas/billing",
)

# Phase 3 slice 2: direct import from mise galleries.
# Set ARGUS_MISE_MEDIA_ROOT to the mise DATA_DIR/media (or equivalent) so that
# --mise-gallery-id / mise_gallery_id= can auto-resolve to .../<id>/original
# using mise's storage layout (MEDIA_DIR / gallery_id / "original" / stored).
# If unset, caller must pass explicit folder path to the originals.
MISE_MEDIA_ROOT = Path(os.environ.get("ARGUS_MISE_MEDIA_ROOT", "")) if os.environ.get("ARGUS_MISE_MEDIA_ROOT") else None

# Phase 6 slice 1: read-only Mise gallery index (GET /api/galleries on flow).
# BOTH url+token arm path resolution via originals_path when ARGUS_MISE_MEDIA_ROOT
# is unset. Use the same bearer as MISE_ARGUS_TOKEN on the Mise side.
MISE_URL = os.environ.get("ARGUS_MISE_URL", "").rstrip("/")
MISE_API_TOKEN = os.environ.get("ARGUS_MISE_API_TOKEN", "")
MISE_TIMEOUT = int(os.environ.get("ARGUS_MISE_TIMEOUT", "10"))

# Phase 11 hardening — in SaaS mode, folder/path analysis is confined to these
# roots so a tenant API key can't make the server read arbitrary local files.
# Comma-separated. Homelab (non-SaaS) is unrestricted (the operator's own box).
# When unset in SaaS mode, defaults to the mise media root (if set) plus the
# data dir; an empty list in SaaS mode means no local-path analysis at all.
ALLOWED_MEDIA_ROOTS = [
    Path(p.strip()).expanduser()
    for p in os.environ.get("ARGUS_ALLOWED_MEDIA_ROOTS", "").split(",")
    if p.strip()
]
if not ALLOWED_MEDIA_ROOTS:
    ALLOWED_MEDIA_ROOTS = [r for r in (MISE_MEDIA_ROOT, DATA_DIR) if r is not None]

# Logging
LOG_LEVEL = os.environ.get("ARGUS_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("argus")
