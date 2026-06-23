"""Phase 10 tests — SaaS tenants, metering, cloud vision routing (mock only)."""

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
os.environ["ARGUS_SAAS_MODE"] = "true"
os.environ["ARGUS_CLOUD_BACKEND"] = "real"

from app import config, db, tenants  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
ADMIN = {"Authorization": "Bearer phase10-admin"}
TENANT_HEADERS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def saas_env(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    monkeypatch.setattr(config, "API_TOKEN", "phase10-admin")
    monkeypatch.setattr(config, "CLOUD_BACKEND", "real")
    monkeypatch.setattr(config, "VISION_BACKEND", "mock")
    monkeypatch.setattr(config, "CLOUD_COST_CAP_USD", 1.0)
    monkeypatch.setattr(config, "CLOUD_MONTHLY_IMAGE_CAP", 100)
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM tenant_usage")
        con.execute("DELETE FROM tenant_api_keys")
        con.execute("DELETE FROM tenants")
    tenant = tenants.create_tenant(
        "demo",
        name="Demo Tenant",
        vision_provider="grok",
        cost_cap_usd=0.05,
        monthly_image_cap=5,
    )
    issued = tenants.issue_api_key("demo", label="test")
    global TENANT_HEADERS
    TENANT_HEADERS = {"Authorization": f"Bearer {issued['api_key']}"}
    yield


@pytest.fixture(scope="module")
def sample_image() -> str:
    path = Path(_TMP) / "sample.jpg"
    Image.new("RGB", (640, 480), color=(100, 80, 60)).save(path, format="JPEG")
    return str(path)


def test_admin_create_tenant():
    r = client.post(
        "/admin/tenants",
        json={"id": "platekit", "name": "Platekit", "monthly_image_cap": 50},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant"]["id"] == "platekit"


def test_tenant_key_can_analyze(sample_image):
    r = client.post("/analyze", data={"path": sample_image}, headers=TENANT_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["model"].startswith("mock:")


def test_tenant_usage_increments(sample_image):
    client.post("/analyze", data={"path": sample_image}, headers=TENANT_HEADERS)
    usage = client.get("/tenant/usage", headers=TENANT_HEADERS).json()
    assert usage["tenant"]["images_analyzed"] >= 1
    assert usage["tenant"]["cost_usd"] > 0


def test_tenant_cost_cap_blocks(sample_image, monkeypatch):
    monkeypatch.setattr(config, "CLOUD_COST_PER_IMAGE", 0.05)
    db.update_tenant("demo", cost_cap_usd=0.001)
    first = client.post("/analyze", data={"path": sample_image}, headers=TENANT_HEADERS)
    assert first.status_code == 200, first.text
    blocked = client.post("/analyze", data={"path": sample_image}, headers=TENANT_HEADERS)
    assert blocked.status_code == 402


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


def test_issue_key_via_admin():
    r = client.post(
        "/admin/tenants/demo/keys",
        json={"label": "rotated"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    assert "api_key" in r.json()