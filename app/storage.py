"""Tenant object storage — local disk (default) or S3-compatible."""
from __future__ import annotations

import logging
from pathlib import Path

from . import config

log = logging.getLogger("argus.storage")


class StorageError(Exception):
    """Raised when a storage operation fails."""


def _s3_ready() -> bool:
    return bool(
        config.S3_BUCKET
        and config.S3_ACCESS_KEY
        and config.S3_SECRET_KEY
        and config.STORAGE_BACKEND == "s3"
    )


def tenant_key(tenant_id: str, filename: str) -> str:
    safe = Path(filename or "upload.jpg").name
    return f"{config.S3_PREFIX.rstrip('/')}/{tenant_id}/uploads/{safe}"


def save_tenant_upload(tenant_id: str, filename: str, data: bytes) -> str:
    """Persist upload; returns URI (s3:// or file path string)."""
    if config.STORAGE_BACKEND == "s3" and _s3_ready():
        return _save_s3(tenant_id, filename, data)
    return _save_local(tenant_id, filename, data)


def _save_local(tenant_id: str, filename: str, data: bytes) -> str:
    root = config.DATA_DIR / "tenants" / tenant_id / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name
    path = root / safe
    if path.exists():
        stem = path.stem
        suffix = path.suffix
        path = root / f"{stem}-{len(data)}{suffix}"
    path.write_bytes(data)
    return str(path.resolve())


def _save_s3(tenant_id: str, filename: str, data: bytes) -> str:
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:
        raise StorageError("boto3 required for ARGUS_STORAGE_BACKEND=s3") from exc

    key = tenant_key(tenant_id, filename)
    client_kwargs: dict = {
        "service_name": "s3",
        "region_name": config.S3_REGION,
        "aws_access_key_id": config.S3_ACCESS_KEY,
        "aws_secret_access_key": config.S3_SECRET_KEY,
        "config": BotoConfig(signature_version="s3v4"),
    }
    if config.S3_ENDPOINT:
        client_kwargs["endpoint_url"] = config.S3_ENDPOINT
    client = boto3.client(**client_kwargs)
    client.put_object(
        Bucket=config.S3_BUCKET,
        Key=key,
        Body=data,
        ContentType="application/octet-stream",
    )
    uri = f"s3://{config.S3_BUCKET}/{key}"
    log.info("stored tenant upload %s", uri)
    return uri


def resolve_upload_path(stored: str) -> Path:
    """Return local Path for analysis. S3 objects are downloaded to a cache file."""
    if stored.startswith("s3://"):
        return _materialize_s3(stored)
    return Path(stored).expanduser().resolve()


def _materialize_s3(uri: str) -> Path:
    # s3://bucket/key
    without = uri.removeprefix("s3://")
    bucket, _, key = without.partition("/")
    cache_root = config.DATA_DIR / "s3_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = cache_root / key.replace("/", "__")
    if cache_path.exists():
        return cache_path
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:
        raise StorageError("boto3 required to read s3 uploads") from exc
    client_kwargs: dict = {
        "service_name": "s3",
        "region_name": config.S3_REGION,
        "aws_access_key_id": config.S3_ACCESS_KEY,
        "aws_secret_access_key": config.S3_SECRET_KEY,
        "config": BotoConfig(signature_version="s3v4"),
    }
    if config.S3_ENDPOINT:
        client_kwargs["endpoint_url"] = config.S3_ENDPOINT
    client = boto3.client(**client_kwargs)
    obj = client.get_object(Bucket=bucket, Key=key)
    data = obj["Body"].read()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return cache_path