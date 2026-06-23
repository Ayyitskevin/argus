"""Vision backend readiness — config snapshot for /vision/status and the home UI."""
from __future__ import annotations

from . import config, metrics


def vision_status() -> dict:
    """Return operator-facing vision configuration and readiness."""
    backend = config.VISION_BACKEND
    key_configured = bool(config.XAI_API_KEY)
    provider = "xai" if backend == "grok" else backend

    if backend == "mock":
        ready = True
        message = "Mock backend — no xAI calls; safe for CI and local dev."
    elif backend == "grok":
        if not key_configured:
            ready = False
            message = "Grok backend selected but XAI_API_KEY is not set."
        else:
            ready = True
            message = "Grok vision configured — run scripts/grok_smoke.py to verify credits."
    else:
        ready = False
        message = f"Unsupported vision backend: {backend}"

    snap = metrics.snapshot()
    grok_counters = {
        k: v
        for k, v in snap["counters"].items()
        if k.startswith("grok_")
    }

    return {
        "backend": backend,
        "provider": provider,
        "model": config.VISION_MODEL,
        "api_key_configured": key_configured,
        "ready": ready,
        "message": message,
        "cost_tracking": config.COST_TRACKING,
        "estimated_cost_per_image_usd": config.CLOUD_COST_PER_IMAGE,
        "grok_usage": grok_counters,
        "grok_cost_usd": snap["gauges"].get("grok_cost_usd", 0.0),
    }