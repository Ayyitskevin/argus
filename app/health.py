"""Dependency checks for /healthz."""
from __future__ import annotations

from typing import Any

from . import billing, config, db


def _check_database() -> dict[str, str]:
    try:
        if db.ping():
            return {"status": "ok"}
        return {"status": "error", "detail": "ping failed"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


def _check_queue(worker: Any | None) -> dict[str, str]:
    if not config.QUEUE_ENABLED:
        return {"status": "disabled"}
    worker_running = bool(worker and getattr(worker, "is_running", lambda: False)())
    depth = db.queue_depth()
    active = db.count_jobs_by_status("running")
    failed = db.count_jobs_by_status("failed")
    dead_letter = db.count_jobs_by_status("dead_letter")
    status = "ok" if worker_running else "degraded"
    if active and not worker_running:
        status = "degraded"
    return {
        "status": status,
        "depth": depth,
        "worker_running": worker_running,
        "running": active,
        "failed": failed,
        "dead_letter": dead_letter,
    }


def _check_vision() -> dict[str, str | bool]:
    backend = config.VISION_BACKEND
    if backend == "mock":
        return {"status": "ok", "backend": backend, "configured": True}
    if backend == "grok":
        # Real path readiness depends on the selected provider, not just xAI.
        if config.VISION_PROVIDER == "qwen":
            configured = bool(config.QWEN_BASE_URL)
            return {
                "status": "ok" if configured else "degraded",
                "backend": backend,
                "provider": "qwen",
                "configured": configured,
            }
        configured = bool(config.XAI_API_KEY)
        return {
            "status": "ok" if configured else "degraded",
            "backend": backend,
            "provider": "grok",
            "configured": configured,
        }
    return {"status": "ok", "backend": backend, "configured": True}


def _check_billing() -> dict[str, str | bool]:
    if not config.SAAS_MODE:
        return {"status": "disabled"}
    enabled = billing.billing_enabled()
    return {
        "status": "ok" if enabled else "disabled",
        "configured": enabled,
        "webhook": bool(config.STRIPE_WEBHOOK_SECRET),
    }


def _check_mise() -> dict[str, str | bool]:
    from . import mise_client

    if not mise_client.is_enabled():
        return {"status": "disabled", "configured": False}
    try:
        mise_client.list_galleries(published=True)
        return {"status": "ok", "configured": True, "reachable": True}
    except Exception as exc:
        return {"status": "degraded", "configured": True, "reachable": False, "detail": str(exc)[:120]}


def _check_plutus() -> dict[str, str | bool]:
    from . import plutus_client

    st = plutus_client.connectivity()
    if not st.get("configured"):
        return {"status": "disabled", "configured": False}
    reachable = bool(st.get("reachable"))
    return {
        "status": "ok" if reachable else "degraded",
        "configured": True,
        "reachable": reachable,
        **{k: v for k, v in st.items() if k not in {"configured", "reachable"}},
    }


def _check_storage() -> dict[str, str | bool]:
    if config.STORAGE_BACKEND == "s3":
        ready = bool(config.S3_BUCKET and config.S3_ACCESS_KEY and config.S3_SECRET_KEY)
        return {"status": "ok" if ready else "degraded", "backend": "s3", "configured": ready}
    return {"status": "ok", "backend": "local", "configured": True}


def build_health_report(*, worker: Any | None = None) -> dict:
    checks = {
        "database": _check_database(),
        "queue": _check_queue(worker),
        "vision": _check_vision(),
        "storage": _check_storage(),
    }
    if not config.SAAS_MODE:
        checks["mise"] = _check_mise()
        checks["plutus"] = _check_plutus()
    if config.SAAS_MODE:
        checks["billing"] = _check_billing()

    critical = [checks["database"]["status"]]
    if config.QUEUE_ENABLED:
        critical.append(checks["queue"]["status"])
    if config.VISION_BACKEND == "grok" and not config.XAI_API_KEY:
        # Degraded vision is acceptable for mock/dev; grok without key is degraded overall.
        pass

    if "error" in critical:
        overall = "error"
    elif any(item.get("status") == "degraded" for item in checks.values()):
        overall = "degraded"
    else:
        overall = "ok"

    return {"status": overall, "checks": checks}