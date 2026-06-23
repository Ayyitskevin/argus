"""Security-fix regression tests.

These pin *why* the fixes matter, not just that code runs:

- Callback URLs are tenant-supplied. In SaaS mode they must be public HTTPS
  only — a loopback/private/tailnet target is an SSRF into the host's own
  network — and the admin API token must NEVER ride along on that request, or a
  tenant could harvest it by pointing the callback at a server they control.
- Preferences and client history are per-tenant. A tenant's read or write must
  not see or clobber another tenant's row that happens to share a client_id.
- /metrics exposes cross-tenant operational data and must be admin-only.
"""
import os
import tempfile
import threading

import pytest

_TMP = tempfile.mkdtemp(prefix="argus-sec-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_CLOUD_BACKEND"] = "real"
os.environ["ARGUS_TENANT_KEY_PEPPER"] = "sec-test-pepper"

from fastapi.testclient import TestClient  # noqa: E402

from app import callbacks, config, db, tenants  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def saas_env(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    monkeypatch.setattr(config, "API_TOKEN", "sec-admin")
    monkeypatch.setattr(config, "TENANT_KEY_PEPPER", "sec-test-pepper")
    db.init()
    with db.tx() as con:
        # FK-safe order: children before parents.
        con.execute("DELETE FROM photo_analyses")
        con.execute("DELETE FROM preferences")
        con.execute("DELETE FROM analysis_runs")
        con.execute("DELETE FROM tenant_usage")
        con.execute("DELETE FROM cap_alert_log")
        con.execute("DELETE FROM tenant_api_keys")
        con.execute("DELETE FROM tenants")
    tenants.create_tenant("acme", name="Acme")
    a_key = tenants.issue_api_key("acme", label="t")["api_key"]
    tenants.create_tenant("globex", name="Globex")
    b_key = tenants.issue_api_key("globex", label="t")["api_key"]
    set_auth_context(None)
    yield {
        "a": {"Authorization": f"Bearer {a_key}"},
        "b": {"Authorization": f"Bearer {b_key}"},
    }
    set_auth_context(None)


# --- callback guard: SaaS denies SSRF targets -------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/cb",          # cleartext rejected
        "https://localhost/cb",
        "https://127.0.0.1/cb",
        "https://[::1]/cb",
        "https://10.0.0.5/cb",
        "https://192.168.1.10/cb",
        "https://169.254.169.254/latest",  # cloud metadata endpoint
        "https://mickey.ts.net/cb",        # tailnet target
    ],
)
def test_saas_callback_guard_denies_private_and_cleartext(monkeypatch, url):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    assert callbacks.is_allowed_callback_url(url) is False


def test_saas_callback_guard_allows_public_https(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    assert callbacks.is_allowed_callback_url("https://8.8.8.8/cb") is True


def test_homelab_callback_guard_still_allows_tailnet(monkeypatch):
    # Non-SaaS keeps the homelab behaviour: local/tailnet callbacks are fine.
    monkeypatch.setattr(config, "SAAS_MODE", False)
    assert callbacks.is_allowed_callback_url("http://localhost/cb") is True
    assert callbacks.is_allowed_callback_url("https://mickey.ts.net/cb") is True


def test_fire_callback_never_sends_admin_token(monkeypatch):
    captured = {}
    done = threading.Event()

    def fake_post(url, json=None, timeout=None, headers=None, follow_redirects=None):
        captured["headers"] = headers
        captured["url"] = url
        done.set()

        class _Resp:
            def raise_for_status(self):
                return None

        return _Resp()

    monkeypatch.setattr(config, "SAAS_MODE", True)
    monkeypatch.setattr(config, "API_TOKEN", "super-secret-admin")
    monkeypatch.setattr(callbacks.httpx, "post", fake_post)

    callbacks.fire_job_callback(
        {"id": "j1", "callback_url": "https://8.8.8.8/cb"}, status="done"
    )
    assert done.wait(timeout=3.0)
    # The admin token must not appear in any outbound header.
    sent = captured.get("headers") or {}
    assert "Authorization" not in sent
    assert "super-secret-admin" not in str(sent)


# --- preferences tenant isolation (db layer) --------------------------------


def test_preferences_are_isolated_by_tenant():
    db.set_preferences("shared-client", {"v": "acme"}, tenant_id="acme")
    db.set_preferences("shared-client", {"v": "globex"}, tenant_id="globex")

    assert db.get_preferences("shared-client", tenant_id="acme") == {"v": "acme"}
    assert db.get_preferences("shared-client", tenant_id="globex") == {"v": "globex"}


def test_one_tenant_cannot_overwrite_anothers_prefs():
    db.set_preferences("c1", {"owner": "acme"}, tenant_id="acme")
    db.set_preferences("c1", {"owner": "globex"}, tenant_id="globex")
    # globex's write must not have deleted acme's row.
    assert db.get_preferences("c1", tenant_id="acme") == {"owner": "acme"}


def test_client_history_is_scoped_by_tenant():
    with db.tx() as con:
        con.execute(
            "INSERT INTO analysis_runs (source, tenant_id) VALUES (?, ?)",
            ("client:bob photos", "acme"),
        )
        con.execute(
            "INSERT INTO analysis_runs (source, tenant_id) VALUES (?, ?)",
            ("client:bob photos", "globex"),
        )
    acme_stats = db.get_client_history_stats("bob", tenant_id="acme")
    globex_stats = db.get_client_history_stats("bob", tenant_id="globex")
    assert acme_stats["num_runs"] == 1
    assert globex_stats["num_runs"] == 1


# --- route-level auth gates --------------------------------------------------


def test_preferences_get_requires_auth(saas_env):
    assert client.get("/preferences", params={"client_id": "x"}).status_code in (401, 403)


def test_preferences_route_does_not_leak_across_tenants(saas_env):
    client.post(
        "/preferences",
        data={"client_id": "p1", "prefs": '{"v": "acme"}'},
        headers=saas_env["a"],
    )
    r = client.get("/preferences", params={"client_id": "p1"}, headers=saas_env["b"])
    assert r.status_code == 200
    assert r.json()["prefs"] == {}  # globex sees nothing acme wrote
