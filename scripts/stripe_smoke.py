#!/usr/bin/env python3
"""Smoke-test Stripe checkout session for a tenant (test mode only)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)

os.environ.setdefault("ARGUS_SAAS_MODE", "true")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default="demo", help="tenant id")
    args = parser.parse_args()

    from app import config, db, billing, tenants

    if not billing.billing_enabled():
        print("STRIPE_SECRET_KEY + STRIPE_PRICE_ID required", file=sys.stderr)
        return 1
    if not billing.stripe_test_mode():
        print("Refusing smoke test outside Stripe test mode", file=sys.stderr)
        return 1

    db.init()
    if not db.get_tenant(args.tenant):
        tenants.create_tenant(args.tenant, name=args.tenant.title())

    session = billing.create_checkout_session(args.tenant)
    print(json.dumps(session, indent=2))
    print()
    print("Open checkout_url in browser. After payment, webhook activates tenant.")
    print(f"Billing status: {billing.billing_status()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())