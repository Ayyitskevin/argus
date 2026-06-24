"""Homelab hand-off to Plutus after Mise gallery analyze completes."""
from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config, mise_client

log = logging.getLogger("argus.plutus")


class PlutusClientError(Exception):
    """Human-readable Plutus API failure."""


def is_enabled() -> bool:
    return bool(config.PLUTUS_URL and config.PLUTUS_TOKEN)


def connectivity() -> dict[str, Any]:
    if not is_enabled():
        return {"configured": False, "reachable": False}
    try:
        with urllib.request.urlopen(
            f"{config.PLUTUS_URL}/healthz", timeout=min(config.PLUTUS_TIMEOUT, 10)
        ) as resp:
            reachable = resp.status == 200
    except Exception as exc:
        return {"configured": True, "reachable": False, "detail": str(exc)[:200]}
    return {"configured": True, "reachable": reachable}


def recommend_mise_gallery(mise_gallery_id: int, *, argus_run_id: int | None = None) -> dict:
    if not is_enabled():
        raise PlutusClientError("Plutus is not configured")
    fields: dict[str, str] = {"mise_gallery_id": str(mise_gallery_id)}
    if argus_run_id is not None:
        fields["argus_run_id"] = str(argus_run_id)
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"{config.PLUTUS_URL}/recommend/mise-gallery",
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {config.PLUTUS_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.PLUTUS_TIMEOUT) as resp:
            import json

            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            pass
        raise PlutusClientError(
            f"Plutus returned HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = exc.reason if hasattr(exc, "reason") else exc
        raise PlutusClientError(f"Plutus unreachable: {reason}") from exc
    except ValueError as exc:
        raise PlutusClientError("Plutus returned an unreadable response") from exc

    if not isinstance(payload, dict) or not payload.get("run_id"):
        raise PlutusClientError("Plutus response missing run_id")
    return payload


def create_share_link(
    run_id: int,
    *,
    label: str | None = None,
) -> dict:
    if not is_enabled():
        raise PlutusClientError("Plutus is not configured")
    fields: dict[str, str] = {"run_id": str(run_id)}
    if label:
        fields["label"] = label
    if config.PLUTUS_TENANT_ID:
        fields["tenant_id"] = config.PLUTUS_TENANT_ID
        path = "/integrations/offer"
    else:
        path = "/storefront/share-links"
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"{config.PLUTUS_URL}{path}",
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {config.PLUTUS_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.PLUTUS_TIMEOUT) as resp:
            import json

            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            pass
        raise PlutusClientError(
            f"Plutus returned HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = exc.reason if hasattr(exc, "reason") else exc
        raise PlutusClientError(f"Plutus unreachable: {reason}") from exc
    except ValueError as exc:
        raise PlutusClientError("Plutus returned an unreadable response") from exc

    if not isinstance(payload, dict) or not payload.get("public_url"):
        raise PlutusClientError("Plutus response missing public_url")
    return payload


def handoff_async(mise_gallery_id: int, argus_run_id: int) -> None:
    """Best-effort background POST to Plutus (non-blocking for job worker)."""
    if not is_enabled():
        return

    def _post() -> None:
        try:
            result = recommend_mise_gallery(mise_gallery_id, argus_run_id=argus_run_id)
            run_id = int(result.get("run_id") or 0)
            log.info(
                "plutus handoff gallery %s argus_run=%s -> plutus_run=%s bundles=%s",
                mise_gallery_id,
                argus_run_id,
                run_id,
                len(result.get("bundles") or []),
            )
            if run_id:
                mise_client.plutus_callback(mise_gallery_id, run_id=run_id, status="done")
        except PlutusClientError as exc:
            log.warning("plutus handoff failed gallery %s: %s", mise_gallery_id, exc)
            mise_client.plutus_callback(mise_gallery_id, status="error", error=str(exc))
        except Exception as exc:
            log.exception("plutus handoff unexpected failure gallery %s", mise_gallery_id)
            mise_client.plutus_callback(mise_gallery_id, status="error", error=str(exc)[:500])

    threading.Thread(
        target=_post,
        name=f"argus-plutus-{mise_gallery_id}",
        daemon=True,
    ).start()