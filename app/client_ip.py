"""The one place that decides a request's real client IP.

Rate limiting and the audit trail both key off "who is calling," and both used to
read X-Forwarded-For directly — a header any client can set. That let an anonymous
caller forge an IP to evade per-IP limits or poison another address's bucket, and
let an attacker stamp someone else's IP onto an audited action. Proxy headers are
trustworthy only when the request actually passed through that proxy, so this
module trusts one ONLY when config opts in (ARGUS_RATE_LIMIT_TRUSTED_PROXY) and
otherwise falls back to the socket peer, which a remote client cannot spoof.
"""
from __future__ import annotations

from fastapi import Request

from . import config


def client_ip(request: Request) -> str:
    """The caller's IP, honoring a trusted-proxy header only when configured."""
    mode = config.RATE_LIMIT_TRUSTED_PROXY
    if mode == "cloudflare":
        cf = request.headers.get("cf-connecting-ip", "").strip()
        if cf:
            return cf
    elif mode == "xff":
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"
