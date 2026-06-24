"""Structured audit trail for SaaS admin and compliance."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from . import config, db
from .client_ip import client_ip

log = logging.getLogger("argus.audit")


def _actor_label(ctx) -> str | None:
    if ctx is None:
        return None
    if getattr(ctx, "is_admin", False):
        return "admin"
    if getattr(ctx, "tenant_id", None):
        return f"tenant:{ctx.tenant_id}"
    return None


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    return client_ip(request)


def record(
    action: str,
    *,
    request: Request | None = None,
    ctx=None,
    tenant_id: str | None = None,
    resource: str | None = None,
    status: str = "ok",
    detail: dict[str, Any] | str | None = None,
) -> None:
    if not config.AUDIT_LOG_ENABLED:
        return
    tid = tenant_id
    if ctx is not None and not tid and getattr(ctx, "tenant_id", None):
        tid = ctx.tenant_id
    actor = _actor_label(ctx)
    try:
        db.insert_audit_event(
            action=action,
            tenant_id=tid,
            actor=actor,
            resource=resource,
            status=status,
            detail=detail,
            ip=_client_ip(request),
        )
    except Exception:
        log.exception("audit log write failed for action=%s", action)