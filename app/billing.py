"""Stripe billing hooks for SaaS tenants (optional)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx

from . import config, db

log = logging.getLogger("argus.billing")

STRIPE_API = "https://api.stripe.com/v1"


class BillingError(Exception):
    """Raised when billing configuration or Stripe calls fail."""


def _stripe_value_ok(value: str | None) -> bool:
    if not value or not str(value).strip():
        return False
    upper = str(value).upper()
    return "CHANGE_ME" not in upper


def billing_enabled() -> bool:
    return _stripe_value_ok(config.STRIPE_SECRET_KEY) and _stripe_value_ok(config.STRIPE_PRICE_ID)


def stripe_test_mode() -> bool:
    key = config.STRIPE_SECRET_KEY or ""
    return key.startswith("sk_test_") or key.startswith("rk_test_")


def billing_status() -> dict:
    return {
        "enabled": billing_enabled(),
        "test_mode": stripe_test_mode(),
        "price_id": config.STRIPE_PRICE_ID,
        "webhook_configured": bool(config.STRIPE_WEBHOOK_SECRET),
        "success_url": config.STRIPE_SUCCESS_URL,
        "cancel_url": config.STRIPE_CANCEL_URL,
    }


def _stripe_request(method: str, path: str, data: dict | None = None) -> dict:
    if not config.STRIPE_SECRET_KEY:
        raise BillingError("STRIPE_SECRET_KEY is not set")
    url = f"{STRIPE_API}{path}"
    with httpx.Client(timeout=30.0) as client:
        resp = client.request(
            method,
            url,
            data=data,
            auth=(config.STRIPE_SECRET_KEY, ""),
        )
    if resp.status_code >= 400:
        raise BillingError(f"Stripe HTTP {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def ensure_stripe_customer(tenant_id: str) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        raise BillingError(f"tenant not found: {tenant_id}")
    existing = tenant.get("stripe_customer_id")
    if existing:
        return existing
    body = {
        "name": tenant["name"],
        "metadata[tenant_id]": tenant_id,
    }
    customer = _stripe_request("POST", "/customers", body)
    customer_id = customer["id"]
    db.update_tenant(
        tenant_id,
        stripe_customer_id=customer_id,
        billing_status=tenant.get("billing_status") or "pending",
    )
    return customer_id


def create_checkout_session(tenant_id: str) -> dict:
    if not billing_enabled():
        raise BillingError("Stripe billing is not configured (STRIPE_SECRET_KEY + STRIPE_PRICE_ID)")
    customer_id = ensure_stripe_customer(tenant_id)
    session = _stripe_request(
        "POST",
        "/checkout/sessions",
        {
            "mode": "subscription",
            "customer": customer_id,
            "line_items[0][price]": config.STRIPE_PRICE_ID,
            "line_items[0][quantity]": "1",
            "success_url": config.STRIPE_SUCCESS_URL,
            "cancel_url": config.STRIPE_CANCEL_URL,
            "metadata[tenant_id]": tenant_id,
            "subscription_data[metadata][tenant_id]": tenant_id,
        },
    )
    return {"checkout_url": session["url"], "session_id": session["id"]}


def create_billing_portal_session(tenant_id: str) -> dict:
    if not billing_enabled():
        raise BillingError("Stripe billing is not configured")
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        raise BillingError(f"tenant not found: {tenant_id}")
    customer_id = tenant.get("stripe_customer_id") or ensure_stripe_customer(tenant_id)
    portal = _stripe_request(
        "POST",
        "/billing_portal/sessions",
        {
            "customer": customer_id,
            "return_url": config.STRIPE_BILLING_PORTAL_RETURN_URL,
        },
    )
    return {"portal_url": portal["url"]}


def verify_webhook_signature(payload: bytes, sig_header: str | None) -> bool:
    if not config.STRIPE_WEBHOOK_SECRET or not sig_header:
        return False
    parts = {}
    for item in sig_header.split(","):
        key, _, value = item.partition("=")
        parts[key] = value
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    signed = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(
        config.STRIPE_WEBHOOK_SECRET.encode("utf-8"),
        signed,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def handle_webhook_event(event: dict[str, Any]) -> None:
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    tenant_id = (obj.get("metadata") or {}).get("tenant_id")

    if etype == "checkout.session.completed":
        tenant_id = tenant_id or (obj.get("metadata") or {}).get("tenant_id")
        sub_id = obj.get("subscription")
        customer_id = obj.get("customer")
        if tenant_id:
            db.update_tenant(
                tenant_id,
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                billing_status="active",
                plan_tier="pro",
                monthly_image_cap=500,
                cost_cap_usd=50.0,
            )
            log.info("activated billing for tenant %s", tenant_id)
        return

    if etype in {"customer.subscription.updated", "customer.subscription.created"}:
        tenant_id = tenant_id or (obj.get("metadata") or {}).get("tenant_id")
        status = obj.get("status")
        if tenant_id and status:
            db.update_tenant(
                tenant_id,
                stripe_subscription_id=obj.get("id"),
                billing_status=status,
                active=status in {"active", "trialing"},
            )
        return

    if etype == "customer.subscription.deleted":
        tenant_id = tenant_id or (obj.get("metadata") or {}).get("tenant_id")
        if tenant_id:
            db.update_tenant(
                tenant_id,
                billing_status="canceled",
                plan_tier="free",
                stripe_subscription_id=None,
            )
        return

    log.debug("ignored stripe event %s", etype)