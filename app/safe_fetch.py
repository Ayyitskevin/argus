"""Safely fetch allowlisted remote images for static assets.

Safety rules (non-negotiable):
- HTTPS only, fixed hostname allowlist
- No user-supplied URLs
- Max byte size, content-type check, magic-byte validation
- Limited redirects, re-validated each hop
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger("argus.safe_fetch")

# Curated CDN hosts only — never pass arbitrary URLs into this module.
ALLOWED_IMAGE_HOSTS = frozenset(
    {
        "images.unsplash.com",
        "upload.wikimedia.org",
    }
)

MAX_IMAGE_BYTES = 2 * 1024 * 1024
ALLOWED_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/jpg"}
)

# Pre-approved asset catalog (food / photography theme for Argus SaaS UI).
SAAS_HERO_CATALOG: dict[str, str] = {
    "hero-dining.jpg": "https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=1200&q=80&auto=format&fit=crop",
    "hero-plated.jpg": "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=1200&q=80&auto=format&fit=crop",
    "hero-kitchen.jpg": "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=1200&q=80&auto=format&fit=crop",
}


class SafeFetchError(Exception):
    """Raised when a remote asset fails safety checks."""


def _hostname_allowed(hostname: str) -> bool:
    host = (hostname or "").lower().rstrip(".")
    return host in ALLOWED_IMAGE_HOSTS


def _resolve_is_public(hostname: str) -> None:
    """Block private/link-local targets (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SafeFetchError(f"DNS resolution failed for {hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise SafeFetchError(f"blocked private/reserved address for {hostname}: {ip}")


def _validate_image_bytes(data: bytes) -> str:
    if len(data) > MAX_IMAGE_BYTES:
        raise SafeFetchError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    if len(data) < 12:
        raise SafeFetchError("image too small")
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise SafeFetchError("unrecognized image format (magic bytes)")


def fetch_allowlisted_image(url: str) -> tuple[bytes, str]:
    """Download one pre-approved image URL. Returns (bytes, content_type)."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SafeFetchError("only HTTPS URLs are allowed")
    if not _hostname_allowed(parsed.hostname or ""):
        raise SafeFetchError(f"hostname not allowlisted: {parsed.hostname}")
    _resolve_is_public(parsed.hostname or "")

    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        current = url
        for _ in range(3):
            parsed = urlparse(current)
            if parsed.scheme != "https" or not _hostname_allowed(parsed.hostname or ""):
                raise SafeFetchError(f"redirect target not allowed: {current}")
            _resolve_is_public(parsed.hostname or "")
            resp = client.get(current)
            if resp.status_code in {301, 302, 303, 307, 308}:
                location = resp.headers.get("location")
                if not location:
                    raise SafeFetchError("redirect without location header")
                current = str(httpx.URL(current).join(location))
                continue
            if resp.status_code >= 400:
                raise SafeFetchError(f"HTTP {resp.status_code} for {current}")
            raw = resp.content
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            magic_type = _validate_image_bytes(raw)
            if ctype and ctype not in ALLOWED_CONTENT_TYPES:
                log.warning("content-type %s overridden by magic bytes (%s)", ctype, magic_type)
            return raw, magic_type
    raise SafeFetchError("too many redirects")


def sync_saas_catalog(dest_dir: Path) -> list[str]:
    """Download catalog images into dest_dir. Returns written paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for filename, url in SAAS_HERO_CATALOG.items():
        data, _ctype = fetch_allowlisted_image(url)
        out = dest_dir / filename
        out.write_bytes(data)
        written.append(str(out))
        log.info("wrote safe asset %s (%s bytes)", out, len(data))
    return written