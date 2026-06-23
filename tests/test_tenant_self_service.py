"""Tenant self-service API keys and rate-limit headers."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-tenant-ss-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_CLOUD_BACKEND"] = "real"
os.environ["ARGUS_TENANT_KEY_PEPPER"] = "tenant-ss-pepper"

from app import config, db, rate_limit, tenants  # noqa: E402
from app.auth import UI_TOKEN_COOKIE  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def saas_env(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    monkeypatch.setattr(config, "API_TOKEN", "ss-admin")
    monkeypatch.setattr(config, "TENANT_KEY_PEPPER", "tenant-ss-pepper")
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_ANALYZE_PER_MINUTE", 3)
    db._SCHEMA_READY = False
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM cap_alert_log")
        con.execute("DELETE FROM tenant_usage")
        con.execute("DELETE FROM audit_log")
        con.execute("DELETE FROM tenant_api_keys")
        con.execute("DELETE FROM tenants")
    tenants.create_tenant("selfsvc", name="Self Service")
    primary = tenants.issue_api_key("selfsvc", label="primary")
    secondary = tenants.issue_api_key("selfsvc", label="secondary")
    set_auth_context(None)
    yield {
        "primary": primary,
        "secondary": secondary,
        "primary_headers": {"Authorization": f"Bearer {primary['api_key']}"},
        "secondary_headers": {"Authorization": f"Bearer {secondary['api_key']}"},
    }
    set_auth_context(None)
    rate_limit._windows.clear()


@pytest.fixture(scope="module")
def sample_image() -> str:
    path = Path(_TMP) / "sample.jpg"
    Image.new("RGB", (320, 240), color=(70, 60, 50)).save(path, format="JPEG")
    return str(path)


def test_tenant_list_keys_marks_current(saas_env):
    r = client.get("/tenant/keys", headers=saas_env["primary_headers"])
    assert r.status_code == 200
    keys = r.json()["keys"]
    current = [k for k in keys if k.get("is_current")]
    assert len(current) == 1
    assert current[0]["id"] == saas_env["primary"]["key_id"]


def test_tenant_issue_key_via_api(saas_env):
    r = client.post("/tenant/keys", json={"label": "rotated"}, headers=saas_env["primary_headers"])
    assert r.status_code == 200
    assert "api_key" in r.json()


def test_tenant_cannot_revoke_current_key(saas_env):
    r = client.delete(
        f"/tenant/keys/{saas_env['primary']['key_id']}",
        headers=saas_env["primary_headers"],
    )
    assert r.status_code == 400


def test_tenant_revoke_other_key(saas_env):
    r = client.delete(
        f"/tenant/keys/{saas_env['secondary']['key_id']}",
        headers=saas_env["primary_headers"],
    )
    assert r.status_code == 200
    blocked = client.get("/tenant/profile", headers=saas_env["secondary_headers"])
    assert blocked.status_code == 401


def test_tenant_ui_issue_key(saas_env):
    c = TestClient(app)
    c.cookies.set(UI_TOKEN_COOKIE, saas_env["primary"]["api_key"])
    r = c.post("/ui/saas/app/keys", data={"label": "portal"})
    assert r.status_code == 200
    assert "argus_tk_selfsvc_" in r.text
    assert "copy now" in r.text.lower()


def test_rate_limit_headers_on_success(sample_image, saas_env):
    with open(sample_image, "rb") as handle:
        r = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=saas_env["primary_headers"],
        )
    assert r.status_code == 200
    assert "X-RateLimit-Limit" in r.headers
    assert "X-RateLimit-Remaining" in r.headers


def test_rate_limit_headers_on_429(sample_image, saas_env):
    for _ in range(3):
        with open(sample_image, "rb") as handle:
            client.post(
                "/analyze",
                files={"file": ("sample.jpg", handle, "image/jpeg")},
                headers=saas_env["primary_headers"],
            )
    with open(sample_image, "rb") as handle:
        blocked = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=saas_env["primary_headers"],
        )
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")
    assert blocked.headers.get("X-RateLimit-Remaining") == "0"