"""Admin portal UI — create tenant, patch caps, issue/revoke keys."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_TMP = tempfile.mkdtemp(prefix="argus-admin-ui-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_CLOUD_BACKEND"] = "real"
os.environ["ARGUS_TENANT_KEY_PEPPER"] = "admin-ui-pepper"

from app import config, db, tenants  # noqa: E402
from app.auth import UI_TOKEN_COOKIE  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
ADMIN_TOKEN = "admin-ui-token"


@pytest.fixture(autouse=True)
def saas_env(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    monkeypatch.setattr(config, "API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(config, "TENANT_KEY_PEPPER", "admin-ui-pepper")
    monkeypatch.setattr(config, "AUDIT_LOG_ENABLED", True)
    db._SCHEMA_READY = False
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM audit_log")
        con.execute("DELETE FROM tenant_usage")
        con.execute("DELETE FROM tenant_api_keys")
        con.execute("DELETE FROM tenants")
    set_auth_context(None)
    yield
    set_auth_context(None)


def _admin_client():
    c = TestClient(app)
    c.cookies.set(UI_TOKEN_COOKIE, ADMIN_TOKEN)
    return c


def test_admin_console_requires_login():
    r = client.get("/ui/saas/app/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/saas/login"


def test_admin_console_renders_create_form():
    r = _admin_client().get("/ui/saas/app/admin")
    assert r.status_code == 200
    assert "Create tenant" in r.text
    assert "tenant_admin.py" not in r.text


def test_admin_create_tenant_via_ui():
    c = _admin_client()
    r = c.post(
        "/ui/saas/app/admin/tenants",
        data={
            "tenant_id": "acme-ui",
            "name": "Acme UI",
            "vision_provider": "grok",
            "monthly_image_cap": "25",
            "cost_cap_usd": "1.50",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/saas/app/admin/tenants/acme-ui"
    tenant = db.get_tenant("acme-ui")
    assert tenant is not None
    assert tenant["name"] == "Acme UI"
    assert tenant["monthly_image_cap"] == 25


def test_admin_tenant_detail_and_patch():
    tenants.create_tenant("patchme", name="Patch Me", monthly_image_cap=10)
    c = _admin_client()
    detail = c.get("/ui/saas/app/admin/tenants/patchme")
    assert detail.status_code == 200
    assert "Patch Me" in detail.text
    assert "Issue new key" in detail.text

    patched = c.post(
        "/ui/saas/app/admin/tenants/patchme",
        data={
            "name": "Patched Name",
            "active": "1",
            "vision_provider": "openai",
            "monthly_image_cap": "99",
            "cost_cap_usd": "2.5",
        },
        follow_redirects=False,
    )
    assert patched.status_code == 303
    assert "updated=1" in patched.headers["location"]
    updated = db.get_tenant("patchme")
    assert updated["name"] == "Patched Name"
    assert updated["vision_provider"] == "openai"
    assert updated["monthly_image_cap"] == 99


def test_admin_issue_and_revoke_key_via_ui():
    tenants.create_tenant("keyops", name="Key Ops")
    c = _admin_client()
    issued = c.post(
        "/ui/saas/app/admin/tenants/keyops/keys",
        data={"label": "portal-key"},
    )
    assert issued.status_code == 200
    assert "argus_tk_keyops_" in issued.text
    assert "copy now" in issued.text.lower()

    keys = db.list_tenant_keys("keyops")
    active = [k for k in keys if not k["revoked_at"]]
    assert len(active) == 1

    revoked = c.post(
        f"/ui/saas/app/admin/tenants/keyops/keys/{active[0]['id']}/revoke",
        follow_redirects=False,
    )
    assert revoked.status_code == 303
    assert "revoked=1" in revoked.headers["location"]
    keys_after = db.list_tenant_keys("keyops")
    assert keys_after[0]["revoked_at"] is not None


def test_admin_create_duplicate_shows_error():
    tenants.create_tenant("dupe", name="Dupe")
    c = _admin_client()
    r = c.post(
        "/ui/saas/app/admin/tenants",
        data={"tenant_id": "dupe", "name": "Again"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]