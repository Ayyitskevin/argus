#!/usr/bin/env python3
"""Create Stripe test product + price for Argus Cloud subscriptions.

Requires STRIPE_SECRET_KEY (sk_test_...) in environment or .env.

Usage:
    cd ~/ai-workspace/argus-saas && source .env
    python scripts/stripe_setup.py
    python scripts/stripe_setup.py --write-env   # append STRIPE_PRICE_ID to .env
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stripe test product setup for Argus Cloud")
    parser.add_argument("--write-env", action="store_true", help="Write STRIPE_PRICE_ID into .env")
    parser.add_argument("--amount", type=int, default=2900, help="Monthly price in cents (default $29)")
    args = parser.parse_args()

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key or key.endswith("CHANGE_ME"):
        print("Set STRIPE_SECRET_KEY=sk_test_... in .env first.", file=sys.stderr)
        print("Get keys: https://dashboard.stripe.com/test/apikeys", file=sys.stderr)
        return 1
    if not key.startswith(("sk_test_", "rk_test_")):
        print("Warning: key does not look like test mode — use sk_test_ for development.", file=sys.stderr)

    from app.billing import _stripe_request

    product = _stripe_request(
        "POST",
        "/products",
        {
            "name": "Argus Cloud Pro",
            "description": "Metered vision metadata API for photography teams",
            "metadata[argus_plan]": "pro",
        },
    )
    price = _stripe_request(
        "POST",
        "/prices",
        {
            "product": product["id"],
            "unit_amount": str(args.amount),
            "currency": "usd",
            "recurring[interval]": "month",
            "metadata[argus_plan]": "pro",
        },
    )

    price_id = price["id"]
    print("Created Stripe test resources:")
    print(f"  product_id: {product['id']}")
    print(f"  price_id:   {price_id}")
    print()
    print("Add to .env:")
    print(f"  STRIPE_PRICE_ID={price_id}")
    print()
    print("Webhook forwarding (separate terminal):")
    port = os.environ.get("ARGUS_PORT", "8020")
    print(f"  stripe listen --forward-to localhost:{port}/webhooks/stripe")
    print("  → paste whsec_... into STRIPE_WEBHOOK_SECRET")

    if args.write_env:
        env_path = ROOT / ".env"
        if not env_path.exists():
            env_path = Path.cwd() / ".env"
        text = env_path.read_text() if env_path.exists() else ""
        if "STRIPE_PRICE_ID=" in text:
            lines = []
            for line in text.splitlines():
                if line.startswith("STRIPE_PRICE_ID="):
                    lines.append(f"STRIPE_PRICE_ID={price_id}")
                else:
                    lines.append(line)
            env_path.write_text("\n".join(lines) + "\n")
        else:
            env_path.write_text(text.rstrip() + f"\nSTRIPE_PRICE_ID={price_id}\n")
        print(f"Updated {env_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())