"""Per-tenant and per-IP rate limiting for SaaS mode."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import config
from .auth import resolve_auth
from .auth_context import AuthContext

log = logging.getLogger("argus.rate_limit")

_lock = Lock()
_windows: dict[str, deque[float]] = defaultdict(deque)
_redis_client = None
_redis_unavailable = False

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


def _get_redis():
    global _redis_client, _redis_unavailable
    if _redis_unavailable or not config.REDIS_URL:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
    except ImportError:
        log.warning("ARGUS_REDIS_URL set but redis package not installed — in-memory limits")
        _redis_unavailable = True
        return None
    try:
        _redis_client = redis.from_url(config.REDIS_URL, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception as exc:
        log.warning("Redis rate-limit backend unavailable (%s) — in-memory limits", exc)
        _redis_unavailable = True
        return None


def _check_memory(key: str, limit: int) -> tuple[bool, int]:
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


def _check_redis(key: str, limit: int) -> tuple[bool, int]:
    client = _get_redis()
    if client is None:
        return _check_memory(key, limit)

    now = time.time()
    bucket = int(now // 60)
    redis_key = f"argus:rl:{key}:{bucket}"
    try:
        count = client.incr(redis_key)
        if count == 1:
            client.expire(redis_key, 120)
        if count > limit:
            retry_after = int(60 - (now % 60)) + 1
            return False, max(retry_after, 1)
        return True, 0
    except Exception as exc:
        log.warning("redis rate-limit error (%s) — falling back to memory", exc)
        return _check_memory(key, limit)


def _check(key: str, limit: int) -> tuple[bool, int]:
    if config.REDIS_URL:
        return _check_redis(key, limit)
    return _check_memory(key, limit)


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