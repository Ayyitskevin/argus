"""Job completion callbacks (Phase 8) — tailnet-local POST on done/failed."""
from __future__ import annotations

import ipaddress
import logging
import threading
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger("argus.callbacks")


def is_allowed_callback_url(url: str) -> bool:
    """Restrict callbacks to local/tailnet targets (no arbitrary internet egress)."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if host.endswith(".ts.net"):
        return True
    if config.TAILSCALE_HINT and host == config.TAILSCALE_HINT.lower():
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def fire_job_callback(job: dict, *, status: str, result: dict | None = None, error: str | None = None) -> None:
    """POST callback payload in a background thread (best-effort, non-blocking)."""
    callback_url = job.get("callback_url")
    if not callback_url:
        return
    if not is_allowed_callback_url(callback_url):
        log.warning("skipping disallowed callback_url for job %s", job.get("id"))
        return

    payload = {
        "job_id": job.get("id"),
        "status": status,
        "run_id": job.get("run_id"),
        "result": result,
        "error": error,
        "folder": job.get("folder"),
        "client_id": job.get("client_id"),
    }

    def _post() -> None:
        try:
            headers = {}
            if config.API_TOKEN:
                headers["Authorization"] = f"Bearer {config.API_TOKEN}"
            resp = httpx.post(callback_url, json=payload, timeout=10.0, headers=headers)
            resp.raise_for_status()
            log.info("callback delivered for job %s -> %s", job.get("id"), callback_url)
        except Exception as exc:
            log.warning("callback failed for job %s: %s", job.get("id"), exc)

    threading.Thread(target=_post, name=f"argus-callback-{job.get('id')}", daemon=True).start()