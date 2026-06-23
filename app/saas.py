"""SaaS helpers — tenant isolation, upload paths, and request guards."""
from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import config, db
from .auth import resolve_auth
from .auth_context import AuthContext

log = logging.getLogger("argus.saas")

# Routes that stay public when SAAS_MODE is on.
SAAS_PUBLIC_PATHS = frozenset({
    "/healthz",
    "/saas/status",
    "/vision/status",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/webhooks/stripe",
})

SAAS_PUBLIC_UI_PREFIXES = (
    "/ui/saas",
    "/ui/saas/login",
    "/static/",
)

# Prefixes requiring authentication for non-admin API/UI data access.
SAAS_PROTECTED_PREFIXES = (
    "/runs",
    "/thumb/",
    "/jobs",
    "/preferences",
    "/ui/",
    "/clients/",
    "/metrics",
)

# Routes with their own auth dependencies (analyze, admin, tenant).
SAAS_AUTH_OWNED_PREFIXES = (
    "/analyze",
    "/analyze-folder",
    "/import/",
    "/admin/",
    "/tenant/",
)


def validate_saas_startup() -> None:
    """Warn or fail fast when SaaS mode is misconfigured."""
    if not config.SAAS_MODE:
        return
    if "pytest" in sys.modules:
        return
    if not config.API_TOKEN:
        raise RuntimeError("ARGUS_SAAS_MODE requires ARGUS_API_TOKEN for admin access")
    weak_peppers = {None, "", "argus-dev-pepper"}
    if config.TENANT_KEY_PEPPER in weak_peppers or config.TENANT_KEY_PEPPER == config.API_TOKEN:
        log.warning(
            "ARGUS_TENANT_KEY_PEPPER is weak or equals admin token — set a distinct secret in production"
        )


def tenant_scope(ctx: AuthContext | None) -> str | None:
    """Return tenant_id filter for DB queries; None means admin/unscoped."""
    if not config.SAAS_MODE or ctx is None:
        return None
    if ctx.is_admin:
        return None
    return ctx.tenant_id


def tenant_upload_path(tenant_id: str, filename: str) -> Path:
    """Per-tenant upload directory under DATA_DIR."""
    safe = Path(filename or "upload.jpg").name
    root = config.DATA_DIR / "tenants" / tenant_id / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{uuid.uuid4().hex}_{safe}"


def assert_upload_only(ctx: AuthContext, *, has_file: bool, has_path: bool, has_folder: bool = False) -> None:
    """SaaS tenants may not reference server filesystem paths."""
    if not config.SAAS_MODE or ctx.is_admin:
        return
    if has_path or has_folder:
        raise HTTPException(
            status_code=403,
            detail="SaaS tenants must upload files; local paths are not allowed",
        )
    if not has_file:
        raise HTTPException(status_code=400, detail="file upload required")


def get_full_run_for_ctx(run_id: int, ctx: AuthContext | None) -> dict | None:
    """Tenant-scoped run fetch; returns None when not found or not owned."""
    return db.get_full_run(run_id, tenant_id=tenant_scope(ctx))


def get_job_for_ctx(job_id: str, ctx: AuthContext | None) -> dict | None:
    return db.get_job(job_id, tenant_id=tenant_scope(ctx))


def _path_requires_saas_auth(path: str) -> bool:
    if path in SAAS_PUBLIC_PATHS:
        return False
    if path.startswith("/ui/saas") and not path.startswith("/ui/saas/app"):
        return False
    if any(path.startswith(prefix) for prefix in SAAS_PUBLIC_UI_PREFIXES):
        return False
    if path.startswith("/static"):
        return False
    if any(path.startswith(prefix) for prefix in SAAS_AUTH_OWNED_PREFIXES):
        return False
    if path == "/":
        return True
    return any(path.startswith(prefix) for prefix in SAAS_PROTECTED_PREFIXES)


async def saas_auth_middleware(request: Request, call_next):
    """Require bearer/cookie auth on data routes when SAAS_MODE is enabled."""
    if not config.SAAS_MODE:
        return await call_next(request)

    path = request.url.path
    if not _path_requires_saas_auth(path):
        return await call_next(request)

    if getattr(request.state, "auth", None) is not None:
        return await call_next(request)

    try:
        ctx = resolve_auth(request, authorization=request.headers.get("Authorization"))
    except HTTPException as exc:
        if path.startswith("/ui/") or (path.startswith("/runs/") and "export" not in path and request.method == "GET" and "/photo/" not in path):
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse("Authentication required", status_code=exc.status_code)
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    request.state.auth = ctx
    return await call_next(request)