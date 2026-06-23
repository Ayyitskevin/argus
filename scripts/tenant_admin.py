#!/usr/bin/env python3
"""Phase 10 tenant admin CLI — create tenants and issue API keys.

Requires ARGUS_SAAS_MODE=true and admin ARGUS_API_TOKEN on the running server,
or runs locally against SQLite when used as a DB bootstrap helper.

Local DB mode (no server):
    ARGUS_SAAS_MODE=true python scripts/tenant_admin.py create platekit --name "Platekit"

Server mode:
    python scripts/tenant_admin.py --base-url http://127.0.0.1:8010 \\
        --admin-token "$ARGUS_API_TOKEN" create platekit --name "Platekit"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ARGUS_SAAS_MODE", "true")


def _local_create(args: argparse.Namespace) -> int:
    from app import config, db, tenants

    db.init()
    if not config.SAAS_MODE:
        print("Set ARGUS_SAAS_MODE=true", file=sys.stderr)
        return 1
    tenant = tenants.create_tenant(
        args.id,
        name=args.name or args.id,
        vision_provider=args.provider,
        cost_cap_usd=args.cost_cap,
        monthly_image_cap=args.image_cap,
    )
    print(json.dumps({"tenant": tenant}, indent=2))
    if args.issue_key:
        issued = tenants.issue_api_key(args.id, label=args.key_label or "bootstrap")
        print(json.dumps(issued, indent=2))
    return 0


def _server_request(base_url: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    import httpx

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{base_url.rstrip('/')}{path}"
    with httpx.Client(timeout=30) as client:
        if method == "post":
            resp = client.post(url, json=body or {}, headers=headers)
        elif method == "patch":
            resp = client.patch(url, json=body or {}, headers=headers)
        else:
            resp = client.get(url, headers=headers)
    if resp.status_code >= 400:
        print(resp.text, file=sys.stderr)
        raise SystemExit(resp.status_code)
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus tenant admin")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--admin-token", default=os.environ.get("ARGUS_API_TOKEN"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="create tenant")
    create.add_argument("id")
    create.add_argument("--name", default=None)
    create.add_argument("--provider", default="grok")
    create.add_argument("--cost-cap", type=float, default=None)
    create.add_argument("--image-cap", type=int, default=None)
    create.add_argument("--issue-key", action="store_true")
    create.add_argument("--key-label", default=None)

    key = sub.add_parser("issue-key", help="issue tenant API key")
    key.add_argument("id")
    key.add_argument("--label", default=None)

    lst = sub.add_parser("list", help="list tenants")

    args = parser.parse_args()

    if not args.base_url:
        if args.cmd == "create":
            return _local_create(args)
        print("Local mode supports only: create", file=sys.stderr)
        return 1

    if not args.admin_token:
        print("ARGUS_API_TOKEN / --admin-token required for server mode", file=sys.stderr)
        return 1

    if args.cmd == "create":
        out = _server_request(
            args.base_url,
            args.admin_token,
            "post",
            "/admin/tenants",
            {
                "id": args.id,
                "name": args.name or args.id,
                "vision_provider": args.provider,
                "cost_cap_usd": args.cost_cap,
                "monthly_image_cap": args.image_cap,
            },
        )
        print(json.dumps(out, indent=2))
        if args.issue_key:
            issued = _server_request(
                args.base_url,
                args.admin_token,
                "post",
                f"/admin/tenants/{args.id}/keys",
                {"label": args.key_label or "bootstrap"},
            )
            print(json.dumps(issued, indent=2))
    elif args.cmd == "issue-key":
        out = _server_request(
            args.base_url,
            args.admin_token,
            "post",
            f"/admin/tenants/{args.id}/keys",
            {"label": args.label},
        )
        print(json.dumps(out, indent=2))
    elif args.cmd == "list":
        out = _server_request(args.base_url, args.admin_token, "get", "/admin/tenants")
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())