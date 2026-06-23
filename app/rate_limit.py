"""Per-tenant and per-IP rate limiting for SaaS mode."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse

from fastapi import HTTPException

from . import config
from .auth import resolve_auth
from .auth_context import AuthContext

_lock = Lock()
_windows: dict[str, deque[float]] = defaultdict(deque)

ANALYZE_PATHS = frozenset({"/analyze", "/analyze-folder"})


def _client_key(request: Request, ctx: AuthContext | None) -> str:
    if ctx and ctx.tenant_id:
        return f"tenant:{ctx.tenant_id}"
    if ctx and ctx.is_admin:
        return "admin"
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return f"ip:{forwarded}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def _limit_for(request: Request) -> int:
    if request.url.path in ANALYZE_PATHS:
        return config.RATE_LIMIT_ANALYZE_PER_MINUTE
    return config.RATE_LIMIT_PER_MINUTE


def _check(key: str, limit: int) -> tuple[bool, int]:
    now = time.time()
    window = 60.0
    with _lock:
        bucket = _windows[key]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = int(window - (now - bucket[0])) + 1
            return False, max(retry_after, 1)
        bucket.append(now)
        return True, 0


async def rate_limit_middleware(request: Request, call_next):
    if not config.SAAS_MODE or not config.RATE_LIMIT_ENABLED:
        return await call_next(request)

    ctx: AuthContext | None = getattr(request.state, "auth", None)
    if ctx is None and request.headers.get("Authorization"):
        try:
            ctx = resolve_auth(request, authorization=request.headers.get("Authorization"))
            request.state.auth = ctx
        except HTTPException:
            ctx = None
    key = _client_key(request, ctx)
    limit = _limit_for(request)
    ok, retry_after = _check(key, limit)
    if not ok:
        return JSONResponse(
            {"error": "rate limit exceeded", "retry_after_seconds": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)