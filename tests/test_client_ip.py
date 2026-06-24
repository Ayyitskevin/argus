"""Trusted-proxy client-IP resolution.

These pin *why* it matters: rate limiting and the audit trail key off the caller's
IP, so if a client could forge that IP via X-Forwarded-For it could dodge per-IP
limits, poison another address's bucket, or stamp someone else's IP onto an audited
action. Proxy headers are honored ONLY when Argus is configured to sit behind that
proxy; by default the unspoofable socket peer wins.
"""
from __future__ import annotations

from fastapi import Request

from app import config
from app.client_ip import client_ip


def _request(headers: dict[str, str], peer: str = "203.0.113.9") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": (peer, 12345)})


def test_default_ignores_spoofable_forwarded_headers(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_TRUSTED_PROXY", "")
    req = _request({"x-forwarded-for": "1.2.3.4", "cf-connecting-ip": "5.6.7.8"})
    assert client_ip(req) == "203.0.113.9"


def test_xff_mode_trusts_first_hop(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_TRUSTED_PROXY", "xff")
    req = _request({"x-forwarded-for": "1.2.3.4, 9.9.9.9"})
    assert client_ip(req) == "1.2.3.4"


def test_cloudflare_mode_uses_cf_header_not_spoofed_xff(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_TRUSTED_PROXY", "cloudflare")
    req = _request({"cf-connecting-ip": "5.6.7.8", "x-forwarded-for": "1.2.3.4"})
    assert client_ip(req) == "5.6.7.8"


def test_cloudflare_mode_falls_back_to_peer_without_cf_header(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_TRUSTED_PROXY", "cloudflare")
    req = _request({"x-forwarded-for": "1.2.3.4"})
    assert client_ip(req) == "203.0.113.9"


def test_missing_client_returns_unknown(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_TRUSTED_PROXY", "")
    req = Request({"type": "http", "headers": [], "client": None})
    assert client_ip(req) == "unknown"
