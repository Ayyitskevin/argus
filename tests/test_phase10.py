"""Phase 10 tests — SaaS tenants, metering, isolation, cloud vision (mock only)."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-phase10-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_CLOUD_BACKEND"] = "real"
os.environ["ARGUS_TENANT_KEY_PEPPER"] = "phase10-test-pepper"

from app import config, db, tenants  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
ADMIN = {"Authorization": "Bearer phase10-admin"}


@pytest.fixture(autouse=True)
def saas_env(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    monkeypatch.setattr(config, "API_TOKEN", "phase10-admin")
    monkeypatch.setattr(config, "TENANT_KEY_PEPPER", "phase10-test-pepper")
    monkeypatch.setattr(config, "CLOUD_BACKEND", "real")
    monkeypatch.setattr(config, "VISION_BACKEND", "mock")
    monkeypatch.setattr(config, "CLOUD_COST_CAP_USD", 1.0)
    monkeypatch.setattr(config, "CLOUD_MONTHLY_IMAGE_CAP", 100)
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM tenant_usage")
        con.execute("DELETE FROM tenant_api_keys")
        con.execute("DELETE FROM tenants")
        con.execute("DELETE FROM photo_analyses")
        con.execute("DELETE FROM analysis_runs")
    tenants.create_tenant(
        "demo",
        name="Demo Tenant",
        vision_provider="grok",
        cost_cap_usd=0.05,
        monthly_image_cap=5,
    )
    issued = tenants.issue_api_key("demo", label="test")
    tenant_headers = {"Authorization": f"Bearer {issued['api_key']}"}

    tenants.create_tenant("platekit", name="Platekit")
    other_key = tenants.issue_api_key("platekit", label="other")
    other_headers = {"Authorization": f"Bearer {other_key['api_key']}"}

    set_auth_context(None)
    yield {"tenant": tenant_headers, "other": other_headers}
    set_auth_context(None)


@pytest.fixture(scope="module")
def sample_image() -> str:
    path = Path(_TMP) / "sample.jpg"
    Image.new("RGB", (640, 480), color=(100, 80, 60)).save(path, format="JPEG")
    return str(path)


def test_admin_create_tenant():
    r = client.post(
        "/admin/tenants",
        json={"id": "acme", "name": "Acme", "monthly_image_cap": 50},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant"]["id"] == "acme"


def test_tenant_key_can_analyze(sample_image, saas_env):
    with open(sample_image, "rb") as handle:
        r = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=saas_env["tenant"],
        )
    assert r.status_code == 200, r.text
    assert r.json()["model"].startswith("mock:")


def test_tenant_usage_increments(sample_image, saas_env):
    with open(sample_image, "rb") as handle:
        client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=saas_env["tenant"],
        )
    usage = client.get("/tenant/usage", headers=saas_env["tenant"]).json()
    assert usage["tenant"]["images_analyzed"] >= 1
    assert usage["tenant"]["cost_usd"] > 0


def test_tenant_cost_cap_blocks(sample_image, saas_env, monkeypatch):
    monkeypatch.setattr(config, "CLOUD_COST_PER_IMAGE", 0.05)
    db.update_tenant("demo", cost_cap_usd=0.001)
    with open(sample_image, "rb") as handle:
        first = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=saas_env["tenant"],
        )
    assert first.status_code == 402, first.text
    with open(sample_image, "rb") as handle:
        blocked = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=saas_env["tenant"],
        )
    assert blocked.status_code == 402


def test_saas_rejects_local_path_for_tenant(sample_image, saas_env):
    r = client.post("/analyze", data={"path": sample_image}, headers=saas_env["tenant"])
    assert r.status_code == 403


def test_cross_tenant_run_isolation(sample_image, saas_env):
    with open(sample_image, "rb") as handle:
        created = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=saas_env["tenant"],
        )
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    blocked = client.get(f"/runs/{run_id}/export", headers=saas_env["other"])
    assert blocked.status_code == 404


def test_unauthenticated_runs_blocked():
    r = client.get("/runs")
    assert r.status_code == 401


def test_admin_list_tenants():
    r = client.get("/admin/tenants", headers=ADMIN)
    assert r.status_code == 200
    ids = {t["id"] for t in r.json()["tenants"]}
    assert "demo" in ids


def test_saas_status():
    r = client.get("/saas/status")
    assert r.status_code == 200
    body = r.json()
    assert body["saas_mode"] is True
    assert "grok" in body["providers"]


def test_healthz_reports_saas():
    r = client.get("/healthz")
    assert r.json()["saas_mode"] is True


def test_issue_and_list_keys():
    r = client.post(
        "/admin/tenants/demo/keys",
        json={"label": "rotated"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    key_id = r.json()["key_id"]
    listed = client.get("/admin/tenants/demo/keys", headers=ADMIN)
    assert listed.status_code == 200
    assert key_id in {k["id"] for k in listed.json()["keys"]}


def test_revoke_key_blocks_access(sample_image, saas_env):
    issued = client.post(
        "/admin/tenants/demo/keys",
        json={"label": "revoke-me"},
        headers=ADMIN,
    ).json()
    headers = {"Authorization": f"Bearer {issued['api_key']}"}
    with open(sample_image, "rb") as handle:
        ok = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=headers,
        )
    assert ok.status_code == 200
    revoked = client.delete(
        f"/admin/tenants/demo/keys/{issued['key_id']}",
        headers=ADMIN,
    )
    assert revoked.status_code == 200
    with open(sample_image, "rb") as handle:
        blocked = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=headers,
        )
    assert blocked.status_code == 401