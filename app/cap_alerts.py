"""Soft cap warnings before hard 402 metering blocks."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config, db, structured_log

log = logging.getLogger("argus.cap_alerts")


def _pct(used: float, cap: float) -> float | None:
    if cap <= 0:
        return None
    return used / cap


def tenant_cap_warnings(tenant_id: str) -> list[dict[str, Any]]:
    """Return active cap warnings for a tenant (no side effects)."""
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return []

    usage = db.get_tenant_usage(tenant_id)
    threshold = config.CAP_WARNING_THRESHOLD
    warnings: list[dict[str, Any]] = []

    image_cap = tenant.get("monthly_image_cap")
    if image_cap is not None and int(image_cap) > 0:
        pct = _pct(float(usage["images_analyzed"]), float(image_cap))
        if pct is not None and pct >= threshold:
            warnings.append(
                {
                    "kind": "monthly_images",
                    "used": usage["images_analyzed"],
                    "cap": int(image_cap),
                    "pct": round(pct * 100, 1),
                    "message": (
                        f"{usage['images_analyzed']}/{image_cap} images this month "
                        f"({pct * 100:.0f}% of cap)"
                    ),
                }
            )

    cost_cap = tenant.get("cost_cap_usd")
    if cost_cap is not None and float(cost_cap) > 0:
        pct = _pct(float(usage["cost_usd"]), float(cost_cap))
        if pct is not None and pct >= threshold:
            warnings.append(
                {
                    "kind": "cost_usd",
                    "used": usage["cost_usd"],
                    "cap": float(cost_cap),
                    "pct": round(pct * 100, 1),
                    "message": (
                        f"${usage['cost_usd']:.4f}/${cost_cap:.2f} estimated cost "
                        f"({pct * 100:.0f}% of cap)"
                    ),
                }
            )

    return warnings


def _post_webhook(payload: dict[str, Any]) -> None:
    url = config.CAP_WEBHOOK_URL
    if not url:
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(url, json=payload)
    except Exception as exc:
        log.warning("cap webhook failed: %s", exc)


def maybe_notify(tenant_id: str) -> list[dict[str, Any]]:
    """Log/webhook once per period per warning kind when threshold crossed."""
    sent: list[dict[str, Any]] = []
    period = db._usage_period()
    for warning in tenant_cap_warnings(tenant_id):
        kind = warning["kind"]
        if db.cap_alert_already_sent(tenant_id, period, kind):
            continue
        structured_log.event(
            "cap.warning",
            tenant_id=tenant_id,
            kind=kind,
            pct=warning["pct"],
            used=warning["used"],
            cap=warning["cap"],
        )
        _post_webhook(
            {
                "tenant_id": tenant_id,
                "period": period,
                "warning": warning,
            }
        )
        db.record_cap_alert(tenant_id, period, kind)
        sent.append(warning)
    return sent