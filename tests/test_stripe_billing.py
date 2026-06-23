"""Stripe billing unit tests (no live API calls)."""

import hashlib
import hmac
import json
import time

import pytest

from app import billing, config, db


def test_billing_status_disabled():
    assert billing.billing_status()["enabled"] is False


def test_stripe_test_mode_detection(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_abc")
    assert billing.stripe_test_mode() is True
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_live_abc")
    assert billing.stripe_test_mode() is False


def test_webhook_signature_roundtrip(monkeypatch):
    secret = "whsec_test_secret"
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", secret)
    payload = json.dumps({"type": "checkout.session.completed", "data": {"object": {}}})
    ts = str(int(time.time()))
    signed = f"{ts}.{payload}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    assert billing.verify_webhook_signature(payload.encode("utf-8"), header)


def test_webhook_activates_tenant(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    db.init()
    from app import tenants

    with db.tx() as con:
        con.execute("DELETE FROM tenants WHERE id='billdemo'")
    tenants.create_tenant("billdemo", name="Bill Demo")
    billing.handle_webhook_event(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {"tenant_id": "billdemo"},
                    "customer": "cus_test",
                    "subscription": "sub_test",
                }
            },
        }
    )
    tenant = db.get_tenant("billdemo")
    assert tenant["billing_status"] == "active"
    assert tenant["plan_tier"] == "pro"
    assert tenant["stripe_subscription_id"] == "sub_test"


def test_create_checkout_requires_config(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", None)
    monkeypatch.setattr(config, "STRIPE_PRICE_ID", None)
    with pytest.raises(billing.BillingError):
        billing.create_checkout_session("demo")