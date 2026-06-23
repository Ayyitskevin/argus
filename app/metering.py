"""Phase 10 usage metering and cloud cost caps."""
from __future__ import annotations

from . import config, db
from .db import _UsageCapExceeded


class MeteringError(Exception):
    """Raised when a tenant or global cap would be exceeded."""

    def __init__(self, message: str, status_code: int = 402):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def cloud_metering_enabled() -> bool:
    return config.SAAS_MODE and config.CLOUD_BACKEND in {"simulated", "real", "stub"}


def caps_enforced() -> bool:
    """Whether usage caps block requests (real + simulated backends)."""
    return cloud_metering_enabled() and config.CLOUD_BACKEND in {"real", "simulated"}


def enforce_caps(tenant_id: str | None, *, images: int) -> None:
    """Pre-flight cap check before expensive vision work."""
    if not caps_enforced() or images <= 0:
        return

    global_usage = db.global_usage_totals()
    if config.CLOUD_COST_CAP_USD > 0 and global_usage["cost_usd"] >= config.CLOUD_COST_CAP_USD:
        raise MeteringError("global cloud cost cap reached", 402)
    if (
        config.CLOUD_MONTHLY_IMAGE_CAP > 0
        and global_usage["images_analyzed"] + images > config.CLOUD_MONTHLY_IMAGE_CAP
    ):
        raise MeteringError("global monthly image cap reached", 402)

    if not tenant_id:
        return

    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return

    usage = db.get_tenant_usage(tenant_id)
    projected_cost = usage["cost_usd"] + estimate_cost(images)
    cap_usd = tenant.get("cost_cap_usd")
    if cap_usd is not None and cap_usd > 0 and projected_cost > float(cap_usd):
        raise MeteringError(f"tenant {tenant_id} cost cap reached", 402)

    image_cap = tenant.get("monthly_image_cap")
    if (
        image_cap is not None
        and image_cap > 0
        and usage["images_analyzed"] + images > int(image_cap)
    ):
        raise MeteringError(f"tenant {tenant_id} monthly image cap reached", 402)


def estimate_cost(images: int) -> float:
    return round(images * config.CLOUD_COST_PER_IMAGE, 6)


def record_usage(
    tenant_id: str | None,
    *,
    images: int,
    cost_usd: float | None = None,
    grok_api_calls: int = 0,
) -> dict | None:
    """Atomically increment tenant usage when SaaS metering is active."""
    if not cloud_metering_enabled() or not tenant_id or images <= 0:
        return None

    cost = cost_usd if cost_usd is not None else estimate_cost(images)
    try:
        return db.charge_tenant_usage(
            tenant_id,
            images=images,
            cost_usd=cost,
            grok_api_calls=grok_api_calls,
            global_cost_cap_usd=config.CLOUD_COST_CAP_USD,
            global_monthly_image_cap=config.CLOUD_MONTHLY_IMAGE_CAP,
        )
    except _UsageCapExceeded as exc:
        raise MeteringError(exc.message, 402) from exc


def usage_snapshot(tenant_id: str | None = None) -> dict:
    period_usage = db.get_tenant_usage(tenant_id) if tenant_id else None
    warnings: list[dict] = []
    if tenant_id and config.SAAS_MODE:
        from . import cap_alerts

        warnings = cap_alerts.tenant_cap_warnings(tenant_id)
    return {
        "saas_mode": config.SAAS_MODE,
        "cloud_backend": config.CLOUD_BACKEND,
        "period": db._usage_period(),
        "global": db.global_usage_totals(),
        "tenant": period_usage,
        "warnings": warnings,
        "cap_warning_threshold_pct": round(config.CAP_WARNING_THRESHOLD * 100, 1),
        "caps": {
            "global_cost_usd": config.CLOUD_COST_CAP_USD or None,
            "global_monthly_images": config.CLOUD_MONTHLY_IMAGE_CAP or None,
            "tenant_cost_usd": (db.get_tenant(tenant_id) or {}).get("cost_cap_usd")
            if tenant_id
            else None,
            "tenant_monthly_images": (db.get_tenant(tenant_id) or {}).get("monthly_image_cap")
            if tenant_id
            else None,
        },
    }