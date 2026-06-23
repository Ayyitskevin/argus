"""Optional bearer auth for Argus (Phase 4).

When ARGUS_API_TOKEN is unset, all routes stay open (local dev default).
When set, mutating endpoints require ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from . import config


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency — no-op when API_TOKEN is unset."""
    token = config.API_TOKEN
    if not token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != token:
        raise HTTPException(status_code=401, detail="invalid bearer token")