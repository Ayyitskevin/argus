"""Phase 11 tests — audit, rate limits, safe fetch, portal."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-prod-saas-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_CLOUD_BACKEND"] = "real"
os.environ["ARGUS_TENANT_KEY_PEPPER"] = "prod-saas-pepper"

from app import config, db, rate_limit, safe_fetch, tenants  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def saas_env(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    monkeypatch.setattr(config, "API_TOKEN", "admin-prod")
    monkeypatch.setattr(config, "TENANT_KEY_PEPPER", "prod-saas-pepper")
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_ANALYZE_PER_MINUTE", 2)
    monkeypatch.setattr(config, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    db._SCHEMA_READY = False
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM audit_log")
        con.execute("DELETE FROM tenant_usage")
        con.execute("DELETE FROM tenant_api_keys")
        con.execute("DELETE FROM tenants")
        con.execute("DELETE FROM photo_analyses")
        con.execute("DELETE FROM analysis_runs")
    tenants.create_tenant("demo", name="Demo")
    key = tenants.issue_api_key("demo")["api_key"]
    set_auth_context(None)
    yield {"tenant_key": key, "headers": {"Authorization": f"Bearer {key}"}}
    set_auth_context(None)
    rate_limit._windows.clear()


@pytest.fixture(scope="module")
def sample_image() -> str:
    path = Path(_TMP) / "sample.jpg"
    Image.new("RGB", (320, 240), color=(90, 70, 50)).save(path, format="JPEG")
    return str(path)


def test_saas_landing_public():
    r = client.get("/ui/saas")
    assert r.status_code == 200
    assert "Argus Cloud" in r.text


def test_audit_log_on_analyze(sample_image, saas_env):
    with open(sample_image, "rb") as handle:
        r = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=saas_env["headers"],
        )
    assert r.status_code == 200, r.text
    events = db.list_audit_events(tenant_id="demo", action="analyze.single")
    assert events


def test_rate_limit_blocks_burst(sample_image, saas_env):
    for _ in range(2):
        with open(sample_image, "rb") as handle:
            ok = client.post(
                "/analyze",
                files={"file": ("sample.jpg", handle, "image/jpeg")},
                headers=saas_env["headers"],
            )
        assert ok.status_code == 200, ok.text
    with open(sample_image, "rb") as handle:
        blocked = client.post(
            "/analyze",
            files={"file": ("sample.jpg", handle, "image/jpeg")},
            headers=saas_env["headers"],
        )
    assert blocked.status_code == 429


def test_admin_audit_endpoint(saas_env):
    db.insert_audit_event(action="test.event", actor="admin")
    r = client.get("/admin/audit", headers={"Authorization": "Bearer admin-prod"})
    assert r.status_code == 200
    assert any(e["action"] == "test.event" for e in r.json()["events"])


def test_safe_fetch_rejects_unknown_host():
    with pytest.raises(safe_fetch.SafeFetchError):
        safe_fetch.fetch_allowlisted_image("https://evil.example.com/x.jpg")


def test_safe_fetch_catalog_local(tmp_path, monkeypatch):
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
    with patch("app.safe_fetch.fetch_allowlisted_image", return_value=(fake_jpeg, "image/jpeg")):
        written = safe_fetch.sync_saas_catalog(tmp_path)
    assert len(written) == len(safe_fetch.SAAS_HERO_CATALOG)