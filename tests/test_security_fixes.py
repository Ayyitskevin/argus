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

from app import callbacks, config, db, saas, service, tenants  # noqa: E402
from app.auth_context import AuthContext, set_auth_context  # noqa: E402
from app.main import app  # noqa: E402
from app.vision import AnalysisResult, Culling  # noqa: E402

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


# --- H1: tenant scope is fail-closed in SaaS --------------------------------
#
# The danger is a *future* unscoped query in SaaS silently returning every
# tenant's rows. A missing scope must raise; admin/worker code reads across
# tenants only by opting in with the explicit GLOBAL_SCOPE sentinel.


def test_saas_unscoped_query_fails_closed(saas_env):
    with pytest.raises(db.TenantScopeError):
        db.get_preferences("c", tenant_id=None)
    with pytest.raises(db.TenantScopeError):
        db.get_client_history_stats("c", tenant_id=None)
    with pytest.raises(db.TenantScopeError):
        db.list_recent_runs(tenant_id=None)
    with pytest.raises(db.TenantScopeError):
        db.list_jobs(tenant_id=None)


def test_saas_global_scope_sentinel_reads_across_tenants(saas_env):
    db.set_preferences("c1", {"v": "acme"}, tenant_id="acme")
    # Explicit global view does not raise and is genuinely unscoped.
    assert db.get_preferences("c1", tenant_id=db.GLOBAL_SCOPE) == {"v": "acme"}
    assert isinstance(db.list_jobs(tenant_id=db.GLOBAL_SCOPE), list)


def test_admin_scope_resolves_to_global_sentinel(saas_env):
    admin_ctx = AuthContext(is_admin=True)
    assert saas.tenant_scope(admin_ctx) is db.GLOBAL_SCOPE


def test_homelab_unscoped_query_is_allowed(monkeypatch):
    # Homelab has no tenants — None means "no filter", never raises.
    monkeypatch.setattr(config, "SAAS_MODE", False)
    assert db.get_preferences("nobody", tenant_id=None) == {}
    assert saas.tenant_scope(AuthContext(is_admin=True)) is None


# --- H2: local-path analysis is confined to allowed roots in SaaS -----------


def test_saas_path_outside_media_roots_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    monkeypatch.setattr(config, "ALLOWED_MEDIA_ROOTS", [tmp_path])
    inside = tmp_path / "a.jpg"
    inside.write_bytes(b"x")
    # A path under an allowed root is fine; anything else is a 403.
    service.assert_path_within_media_roots(inside)
    with pytest.raises(service.AnalyzeError) as exc:
        service.assert_path_within_media_roots((tmp_path / ".." / "etc" / "passwd"))
    assert exc.value.status_code == 403


def test_homelab_path_is_unrestricted(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", False)
    # Non-SaaS is the operator's own box — no confinement.
    service.assert_path_within_media_roots(__import__("pathlib").Path("/etc/hostname"))


# --- H3: money stored as integer micro-USD, no float drift ------------------


def test_micro_usd_round_trip_holds_sub_cent_costs():
    assert db._usd_to_micros(0.00123) == 1230
    assert db._micros_to_usd(1230) == 0.00123


def test_integer_ledger_does_not_drift_on_sub_cent_charges(saas_env):
    # 1000 sub-cent charges must sum to *exactly* $1.23, not a float-drifted
    # 1.2299999. This is the whole reason the ledger is integer micro-USD.
    tenants.create_tenant("micro", name="Micro")
    for _ in range(1000):
        db.increment_tenant_usage("micro", images=1, cost_usd=0.00123)
    assert db.get_tenant_usage("micro")["cost_usd"] == 1.23


def test_cost_cap_round_trips_through_micro_storage(saas_env):
    tenants.create_tenant("capped", name="Capped", cost_cap_usd=1.5)
    assert db.get_tenant("capped")["cost_cap_usd"] == 1.5


# --- H4: a failed vision analysis must fail the job, not be recorded "done" --


def _failed_result(path: str) -> AnalysisResult:
    return AnalysisResult(
        image_path=path,
        culling=Culling(keeper_score=0.0, technical_quality="unknown", notes="model error"),
        analysis_failed=True,
    )


def test_failed_single_analysis_raises_not_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SAAS_MODE", False)  # H4 is mode-independent
    img = tmp_path / "x.jpg"
    img.write_bytes(b"x")
    monkeypatch.setattr(service.vision, "analyze_image", lambda *a, **k: _failed_result(str(img)))
    with pytest.raises(service.AnalyzeError) as exc:
        service.analyze_single_image(image_path=img)
    assert exc.value.status_code == 502


def test_all_failed_folder_run_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SAAS_MODE", False)
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"y")
    monkeypatch.setattr(
        service.vision,
        "analyze_folder",
        lambda *a, **k: [_failed_result("a.jpg"), _failed_result("b.jpg")],
    )
    with pytest.raises(service.AnalyzeError) as exc:
        service.analyze_folder_run(folder=tmp_path, source=str(tmp_path), limit=10)
    assert exc.value.status_code == 502
