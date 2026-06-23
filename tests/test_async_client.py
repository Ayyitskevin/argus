"""AsyncArgusClient tests — mock httpx only."""

import json

import httpx
import pytest

from app.async_client import AsyncArgusClient


@pytest.mark.asyncio
async def test_async_analyze_folder(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"run_id": 7, "count": 2, "mode": "sync"}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def post(self, url, data=None, json=None, headers=None):
            calls.append({"url": url, "data": data, "json": json, "headers": headers})
            return FakeResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    async with AsyncArgusClient(base_url="http://argus:8010", api_key="secret") as client:
        body = await client.analyze_folder("/tmp/gallery", limit=3, recursive=True)

    assert body["run_id"] == 7
    assert calls[0]["url"] == "http://argus:8010/analyze-folder"
    assert calls[0]["data"]["recursive"] == "true"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_async_retry_job(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "job_id": "abc", "status": "queued"}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def post(self, url, data=None, json=None, headers=None):
            assert url.endswith("/jobs/abc/retry")
            return FakeResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    async with AsyncArgusClient() as client:
        out = await client.retry_job("abc")
    assert out["status"] == "queued"


@pytest.mark.asyncio
async def test_async_fetch_and_write_sidecars(tmp_path, monkeypatch):
    export_payload = {
        "photos": [
            {
                "id": 1,
                "basename": "hero",
                "keywords": ["plating"],
                "shot_type": "hero_plate",
                "suggested_iptc": {"headline": "Hero"},
            }
        ]
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return export_payload

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def get(self, url, headers=None):
            assert "/export" in url
            return FakeResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    async with AsyncArgusClient() as client:
        written = await client.fetch_and_write_sidecars(5, target_dir=str(tmp_path))

    assert (tmp_path / "hero.argus.json").is_file()
    data = json.loads((tmp_path / "hero.argus.json").read_text())
    assert data["keywords"] == ["plating"]
    assert any(p.endswith("hero.argus.json") for p in written)