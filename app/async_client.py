"""Async HTTP client for Argus — mirrors app.client.ArgusClient (Phase 8)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from .client import ArgusError
from .client_common import build_analyze_folder_form, build_job_payload
from .sidecars import build_xmp


@dataclass
class AsyncArgusConfig:
    base_url: str = "http://127.0.0.1:8010"
    timeout: int = 300
    max_retries: int = 3
    retry_delay: float = 1.0
    default_client_id: Optional[str] = None


class AsyncArgusClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8010",
        timeout: int = 300,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        config: Optional[AsyncArgusConfig] = None,
        api_key: Optional[str] = None,
    ):
        if config:
            self.base_url = config.base_url.rstrip("/")
            self.timeout = config.timeout
            self.max_retries = config.max_retries
            self.retry_delay = config.retry_delay
            self.default_client_id = config.default_client_id
        else:
            self.base_url = base_url.rstrip("/")
            self.timeout = timeout
            self.max_retries = max_retries
            self.retry_delay = retry_delay
            self.default_client_id = None
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=self.timeout)

    def _auth_headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict | None = None,
        json_body: dict | None = None,
        method_name: str = "",
    ) -> dict[str, Any]:
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                headers = self._auth_headers()
                if method == "post":
                    if json_body is not None:
                        resp = await self._client.post(url, json=json_body, headers=headers)
                    else:
                        resp = await self._client.post(url, data=data or {}, headers=headers)
                else:
                    resp = await self._client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_err = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (2 ** attempt))
                else:
                    raise ArgusError(
                        f"{method_name} failed after {self.max_retries} attempts: {exc}"
                    ) from exc
        raise ArgusError(f"{method_name} failed: {last_err}") from last_err

    async def __aenter__(self) -> "AsyncArgusClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def analyze_folder(
        self,
        folder: Optional[str] = None,
        *,
        model: Optional[str] = None,
        limit: int = 20,
        write_sidecars: bool = False,
        sidecar_dir: Optional[str] = None,
        mise_gallery_id: Optional[int] = None,
        mise_project_id: Optional[int] = None,
        client_id: Optional[str] = None,
        recursive: bool = False,
        callback_url: Optional[str] = None,
    ) -> dict[str, Any]:
        data = build_analyze_folder_form(
            folder=folder,
            model=model,
            limit=limit,
            write_sidecars=write_sidecars,
            sidecar_dir=sidecar_dir,
            mise_gallery_id=mise_gallery_id,
            mise_project_id=mise_project_id,
            client_id=client_id or self.default_client_id,
            recursive=recursive,
            callback_url=callback_url,
        )
        return await self._request(
            "post",
            f"{self.base_url}/analyze-folder",
            data=data,
            method_name="analyze_folder",
        )

    async def create_job(
        self,
        folder: str,
        *,
        limit: int = 20,
        write_sidecars: bool = False,
        sidecar_dir: Optional[str] = None,
        client_id: Optional[str] = None,
        callback_url: Optional[str] = None,
        recursive: bool = False,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = build_job_payload(
            folder=folder,
            limit=limit,
            write_sidecars=write_sidecars,
            sidecar_dir=sidecar_dir,
            client_id=client_id or self.default_client_id,
            callback_url=callback_url,
            recursive=recursive,
            model=model,
        )
        return await self._request(
            "post",
            f"{self.base_url}/jobs",
            json_body=payload,
            method_name="create_job",
        )

    async def get_run(self, run_id: int) -> dict[str, Any]:
        return await self._request(
            "get",
            f"{self.base_url}/runs/{run_id}/export",
            method_name="get_run",
        )

    async def get_run_manifest(self, run_id: int, sidecar_dir: Optional[str] = None) -> dict[str, Any]:
        url = f"{self.base_url}/runs/{run_id}/manifest.json"
        if sidecar_dir:
            url += f"?sidecar_dir={sidecar_dir}"
        return await self._request("get", url, method_name="get_run_manifest")

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request(
            "get",
            f"{self.base_url}/jobs/{job_id}",
            method_name="get_job",
        )

    async def retry_job(self, job_id: str) -> dict[str, Any]:
        return await self._request(
            "post",
            f"{self.base_url}/jobs/{job_id}/retry",
            json_body={},
            method_name="retry_job",
        )

    async def poll_job(self, job_id: str, max_wait: int = 300, interval: float = 1.0) -> dict:
        for _ in range(int(max_wait / interval)):
            job = await self.get_job(job_id)
            if job["status"] in ("done", "failed", "dead_letter"):
                return job
            await asyncio.sleep(interval)
        raise ArgusError(f"Job {job_id} did not complete within {max_wait}s")

    async def fetch_and_write_sidecars(self, run_id: int, target_dir: str = ".") -> list[str]:
        data = await self.get_run(run_id)
        written: list[str] = []
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        for photo in data.get("photos", []):
            basename = photo.get("basename", f"photo_{photo.get('id', 'unknown')}")
            sidecar = target / f"{basename}.argus.json"
            sidecar.write_text(json.dumps(photo, indent=2, ensure_ascii=False))
            written.append(str(sidecar))
            if photo.get("suggested_iptc"):
                iptc_sc = target / f"{basename}.iptc.json"
                iptc_sc.write_text(
                    json.dumps(photo["suggested_iptc"], indent=2, ensure_ascii=False)
                )
                written.append(str(iptc_sc))
                xmp = build_xmp(photo)
                if xmp:
                    xmp_sc = target / f"{basename}.xmp"
                    xmp_sc.write_text(xmp, encoding="utf-8")
                    written.append(str(xmp_sc))
        return written