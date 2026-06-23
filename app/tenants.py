"""Phase 10 tenant registry — API keys, hashing, resolution."""
from __future__ import annotations

import hashlib
import secrets
import uuid

from . import config, db

KEY_PREFIX = "argus_tk_"


class TenantError(Exception):
    """Tenant validation or lookup failure."""


def _hash_key(raw_key: str) -> str:
    payload = f"{config.TENANT_KEY_PEPPER}:{raw_key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def generate_api_key(tenant_id: str) -> str:
    # Hex-only token so tenant_id can be parsed via rsplit("_", 1).
    token = secrets.token_hex(24)
    return f"{KEY_PREFIX}{tenant_id}_{token}"


def key_prefix_for_tenant(tenant_id: str) -> str:
    return f"{KEY_PREFIX}{tenant_id}"


def tenant_id_from_key(raw_key: str) -> str | None:
    if not raw_key.startswith(KEY_PREFIX):
        return None
    rest = raw_key[len(KEY_PREFIX) :]
    if "_" not in rest:
        return None
    tenant_id, _token = rest.rsplit("_", 1)
    return tenant_id or None


def create_tenant(
    tenant_id: str,
    *,
    name: str,
    vision_provider: str | None = None,
    cost_cap_usd: float | None = None,
    monthly_image_cap: int | None = None,
) -> dict:
    tid = tenant_id.strip().lower().replace(" ", "-")
    if not tid:
        raise TenantError("tenant id required")
    if "_" in tid:
        raise TenantError("tenant id must not contain underscores (reserved for API key format)")
    if db.get_tenant(tid):
        raise TenantError(f"tenant already exists: {tid}")
    return db.create_tenant(
        tid,
        name=name.strip() or tid,
        vision_provider=vision_provider or config.DEFAULT_VISION_PROVIDER,
        cost_cap_usd=cost_cap_usd,
        monthly_image_cap=monthly_image_cap,
    )


def issue_api_key(tenant_id: str, *, label: str | None = None) -> dict:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        raise TenantError(f"tenant not found: {tenant_id}")
    if not tenant["active"]:
        raise TenantError(f"tenant inactive: {tenant_id}")

    raw_key = generate_api_key(tenant_id)
    prefix = key_prefix_for_tenant(tenant_id)
    key_id = str(uuid.uuid4())
    db.insert_tenant_api_key(
        key_id=key_id,
        tenant_id=tenant_id,
        key_prefix=prefix,
        key_hash=_hash_key(raw_key),
        label=label,
    )
    return {
        "key_id": key_id,
        "tenant_id": tenant_id,
        "api_key": raw_key,
        "key_prefix": prefix,
        "label": label,
        "warning": "Store api_key now — it is not persisted in plaintext.",
    }


def resolve_api_key(raw_key: str | None) -> dict | None:
    """Return tenant dict when raw_key matches an active tenant key."""
    if not raw_key or not raw_key.strip():
        return None
    raw_key = raw_key.strip()
    if not raw_key.startswith(KEY_PREFIX):
        return None

    tenant_id = tenant_id_from_key(raw_key)
    if not tenant_id:
        return None
    prefix = key_prefix_for_tenant(tenant_id)
    digest = _hash_key(raw_key)
    for candidate in db.find_tenant_by_key_prefix(prefix):
        if secrets.compare_digest(candidate["key_hash"], digest):
            return candidate["tenant"]
    return None


def revoke_key(key_id: str) -> bool:
    return db.revoke_tenant_api_key(key_id)