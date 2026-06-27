"""401 re-auth / token-drift recovery (PR3). Mocked transport + no-op sleep."""

import os
import tempfile
from pathlib import Path

import httpx
import pytest

_TMP = tempfile.mkdtemp(prefix="argus-reauth-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, mise_client  # noqa: E402

_PAYLOAD = {"idempotency_key": "argus-g7-r42", "run_id": 42, "gallery_id": 7, "status": "done"}
_REAL_HTTPX_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    monkeypatch.setattr(config, "MISE_URL", "http://mise.local")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "tok")
    monkeypatch.setattr(config, "CAP_WEBHOOK_URL", None)
    monkeypatch.setattr(config, "MISE_CALLBACK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("app.mise_client.time.sleep", lambda s: None)
    db._SCHEMA_READY = False
    db.init()
    for row in db.list_dead_letter_callbacks(limit=1000):
        db.resolve_dead_letter_callback(row["idempotency_key"])
    yield


def _mock(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    class _Factory:
        def __init__(self, timeout=None, **kw):
            self._c = _REAL_HTTPX_CLIENT(transport=transport, timeout=timeout)

        def __enter__(self):
            return self._c.__enter__()

        def __exit__(self, *a):
            return self._c.__exit__(*a)

    monkeypatch.setattr("app.mise_client.httpx.Client", _Factory)


def _stub_reload(monkeypatch, *, new_token, counter):
    """Simulate config.reload_mise_token reading a (possibly rotated) token."""
    def _reload():
        counter["n"] += 1
        if new_token is not None:
            config.MISE_API_TOKEN = new_token
        return config.MISE_API_TOKEN

    monkeypatch.setattr(config, "reload_mise_token", _reload)


# --- config.reload_mise_token -------------------------------------------------

def test_reload_reads_rotated_token_from_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_ROOT", tmp_path)
    (tmp_path / ".env").write_text('ARGUS_MISE_API_TOKEN="rotated-123"\nOTHER=x\n', encoding="utf-8")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "stale")
    assert config.reload_mise_token() == "rotated-123"
    assert config.MISE_API_TOKEN == "rotated-123"


def test_reload_keeps_current_when_no_source(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_ROOT", tmp_path)  # no .env here
    monkeypatch.delenv("ARGUS_MISE_API_TOKEN", raising=False)
    monkeypatch.setattr(config, "MISE_API_TOKEN", "current")
    assert config.reload_mise_token() == "current"


# --- 401 re-auth flow ---------------------------------------------------------

def test_401_reload_changed_then_success(monkeypatch):
    counter = {"n": 0}
    _stub_reload(monkeypatch, new_token="newtok", counter=counter)

    def handler(request: httpx.Request) -> httpx.Response:
        # Old token is rejected; the reloaded token is accepted.
        if request.headers.get("Authorization") == "Bearer newtok":
            return httpx.Response(200, json={"ok": True}, request=request)
        return httpx.Response(401, text="expired token", request=request)

    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert counter["n"] == 1  # re-auth attempted exactly once
    assert db.dead_letter_callback_count() == 0  # self-healed, delivered


def test_401_reload_unchanged_dead_letters(monkeypatch):
    counter = {"n": 0}
    _stub_reload(monkeypatch, new_token=None, counter=counter)  # nothing new on disk
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, text="nope", request=request)

    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert calls["n"] == 1  # token unchanged -> no retry POST
    assert counter["n"] == 1
    rows = db.list_dead_letter_callbacks()
    assert len(rows) == 1
    assert rows[0]["last_status"] == "auth"


def test_401_reload_changed_but_still_401_dead_letters(monkeypatch):
    counter = {"n": 0}
    _stub_reload(monkeypatch, new_token="newtok", counter=counter)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, text="still bad", request=request)

    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert counter["n"] == 1  # re-auth attempted only once (no infinite loop)
    assert calls["n"] == 2  # original + one re-auth retry
    assert db.dead_letter_callback_count() == 1


def test_redeliver_reloads_token_then_delivers(monkeypatch):
    # Seed a dead-letter via a transient failure.
    def down(request):
        return httpx.Response(503, text="down", request=request)

    _mock(monkeypatch, down)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert db.dead_letter_callback_count() == 1

    # Operator rotated the token; redelivery reloads it and succeeds.
    counter = {"n": 0}
    _stub_reload(monkeypatch, new_token="newtok", counter=counter)

    def handler(request):
        if request.headers.get("Authorization") == "Bearer newtok":
            return httpx.Response(200, json={"ok": True}, request=request)
        return httpx.Response(401, text="old", request=request)

    _mock(monkeypatch, handler)
    summary = mise_client.redeliver_dead_letters()
    assert counter["n"] == 1  # reloaded once before re-POSTing
    assert summary["delivered"] == 1
    assert db.dead_letter_callback_count() == 0
