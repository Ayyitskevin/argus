"""Job completion callbacks (Phase 8) — tailnet-local POST on done/failed."""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger("argus.callbacks")


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolves_to_public(host: str) -> bool:
    """True only if every DNS answer for host is a public address (SSRF guard
    against a public hostname that resolves into the host's own network)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False
    for addr in addrs:
        try:
            if not _is_public_ip(ipaddress.ip_address(addr)):
                return False
        except ValueError:
            return False
    return True


def is_allowed_callback_url(url: str) -> bool:
    """Decide whether a job callback may POST to this URL.

    Homelab (non-SaaS): the callback target is the operator's own tailnet/LAN,
    so local/private/tailnet hosts are allowed. SaaS: the URL is tenant-supplied,
    so it must be a public HTTPS endpoint — loopback/private/link-local targets
    are an SSRF into the host's own network and are refused, including public
    hostnames that resolve to a private address."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return False

    if config.SAAS_MODE:
        if parsed.scheme != "https":
            return False
        if host == "localhost" or host.endswith(".ts.net"):
            return False
        try:
            return _is_public_ip(ipaddress.ip_address(host))
        except ValueError:
            return _resolves_to_public(host)

    if parsed.scheme not in {"http", "https"}:
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
            # No Authorization header: callback_url is tenant-supplied, so the
            # admin API token must never be attached. follow_redirects is off so
            # a redirect can't bounce the POST to a private SSRF target.
            resp = httpx.post(
                callback_url, json=payload, timeout=10.0, follow_redirects=False
            )
            resp.raise_for_status()
            log.info("callback delivered for job %s -> %s", job.get("id"), callback_url)
        except Exception as exc:
            log.warning("callback failed for job %s: %s", job.get("id"), exc)

    threading.Thread(target=_post, name=f"argus-callback-{job.get('id')}", daemon=True).start()