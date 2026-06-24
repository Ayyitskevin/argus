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


def plutus_callback(
    gallery_id: int,
    *,
    run_id: int | None = None,
    status: str = "done",
    error: str | None = None,
    offer_url: str | None = None,
) -> None:
    """Best-effort write-back so pipeline dashboard shows plutus_last_*."""
    if not is_enabled():
        return
    url = f"{config.MISE_URL}/api/plutus/callback"
    payload: dict[str, Any] = {"status": status}
    if run_id is not None:
        payload["run_id"] = run_id
    if error:
        payload["error"] = error
    if offer_url:
        payload["offer_url"] = offer_url
    try:
        with httpx.Client(timeout=config.MISE_TIMEOUT) as client:
            resp = client.post(
                url,
                params={"gallery_id": gallery_id},
                json=payload,
                headers=_headers(),
            )
    except httpx.RequestError as exc:
        log.warning("mise plutus callback unreachable gallery %s: %s", gallery_id, exc)
        return
    if resp.status_code >= 400:
        log.warning(
            "mise plutus callback HTTP %s gallery %s: %s",
            resp.status_code,
            gallery_id,
            resp.text[:200],
        )