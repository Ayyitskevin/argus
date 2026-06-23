"""Optional bearer auth for Argus (Phase 4+).

When ARGUS_API_TOKEN is unset, all routes stay open (local dev default).
When set, mutating endpoints require a matching token via Authorization header,
UI cookie, or form field (browser flows).
"""
from __future__ import annotations

from fastapi import Header, HTTPException, Request

from . import config

UI_TOKEN_COOKIE = "argus_ui_token"


def token_from_request(
    request: Request,
    *,
    authorization: str | None = None,
    form_token: str | None = None,
) -> str | None:
    """Resolve a bearer token from header, form field, or UI cookie."""
    if form_token and form_token.strip():
        return form_token.strip()
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    cookie = request.cookies.get(UI_TOKEN_COOKIE)
    if cookie and cookie.strip():
        return cookie.strip()
    return None


def verify_api_access(
    request: Request,
    *,
    authorization: str | None = None,
    form_token: str | None = None,
) -> None:
    """Raise 401 when API_TOKEN is set and the caller did not provide it."""
    expected = config.API_TOKEN
    if not expected:
        return
    provided = token_from_request(
        request, authorization=authorization, form_token=form_token
    )
    if not provided:
        raise HTTPException(status_code=401, detail="missing bearer token")
    if provided != expected:
        raise HTTPException(status_code=401, detail="invalid bearer token")


def require_bearer(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency — accepts header or UI cookie."""
    verify_api_access(request, authorization=authorization)