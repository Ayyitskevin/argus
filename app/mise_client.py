"""Read-only Mise gallery index client (Phase 6 slice 1).

Proxies Argus operators to Mise's GET /api/galleries and supplies originals_path
for resolve_mise_folder when ARGUS_MISE_MEDIA_ROOT is unset but homelab paths are
still reachable (shared mount / same host).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config

log = logging.getLogger("argus.mise")


class MiseClientError(Exception):
    """Human-readable Mise API failure (no secrets)."""


def is_enabled() -> bool:
    return bool(config.MISE_URL and config.MISE_API_TOKEN)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.MISE_API_TOKEN}"}


def list_galleries(*, published: bool = True) -> dict[str, Any]:
    if not is_enabled():
        raise MiseClientError("Mise API is not configured")
    url = f"{config.MISE_URL}/api/galleries"
    params = {"published": "true" if published else "false"}
    try:
        with httpx.Client(timeout=config.MISE_TIMEOUT) as client:
            resp = client.get(url, params=params, headers=_headers())
    except httpx.TimeoutException as exc:
        raise MiseClientError(f"Mise API timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise MiseClientError(f"Mise API unreachable: {exc}") from exc

    if resp.status_code == 503:
        raise MiseClientError("Mise galleries API is disarmed")
    if resp.status_code == 401:
        raise MiseClientError("Mise API rejected the bearer token")
    if resp.status_code >= 400:
        raise MiseClientError(f"Mise API returned HTTP {resp.status_code}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise MiseClientError("Mise API returned unreadable JSON") from exc

    if not isinstance(body, dict) or "galleries" not in body:
        raise MiseClientError("Mise API returned an unexpected body")
    return body


def get_gallery(gallery_id: int) -> dict[str, Any] | None:
    body = list_galleries(published=False)
    for row in body.get("galleries") or []:
        if row.get("id") == gallery_id:
            return row
    return None