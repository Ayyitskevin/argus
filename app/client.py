"""Simple Python client for calling a (possibly remote) argus instance over HTTP/tailnet.

Phase 1: designed for mise/mnemosyne integration. Safe to use from other nodes.

Features:
- Retries with backoff on transient errors
- Context manager support
- Sidecar writing helpers (local fetch + write)
- Error handling via ArgusError
- Async stubs (for future aiohttp)

All operations use mock by default on the server side for safety (no model load).

Example (from flow calling argus on mickey over Tailscale):
    from argus.app.client import ArgusClient
    client = ArgusClient("http://mickey:8010")  # Tailscale hostname recommended
    result = client.analyze_folder("/path/to/gallery", limit=10, write_sidecars=True)
    print(result["run_url"])

with ArgusClient(...) as c:
    ...
"""

import json
import time
import httpx
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .sidecars import build_xmp


@dataclass
class ArgusConfig:
    """Simple config for easy embedding of ArgusClient from mnemosyne/mise etc.
    Phase 3 slice 6 polish.
    """
    base_url: str = "http://127.0.0.1:8010"
    timeout: int = 300
    max_retries: int = 3
    retry_delay: float = 1.0
    default_client_id: Optional[str] = None  # auto-forward to calls for prefs etc.


class ArgusError(Exception):
    """Base error for ArgusClient."""
    pass


class ArgusClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8010", timeout: int = 300, max_retries: int = 3, retry_delay: float = 1.0, config: Optional[ArgusConfig] = None):
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
        self._client = httpx.Client(timeout=self.timeout)

    def set_default_client_id(self, client_id: Optional[str]):
        """For easy runtime embedding config from mnemosyne/mise."""
        self.default_client_id = client_id

    def _request(self, method: str, url: str, data: dict = None, files: dict = None, method_name: str = ""):
        """Internal request with simple retry on transient errors using httpx."""
        last_err = None
        for attempt in range(self.max_retries):
            try:
                if method == "post":
                    resp = self._client.post(url, data=data, files=files)
                else:
                    resp = self._client.get(url)
                resp.raise_for_status()
                return resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                last_err = e
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2 ** attempt))
                else:
                    raise ArgusError(f"{method_name} failed after {self.max_retries} attempts: {e}") from e
            except Exception as e:
                raise ArgusError(f"{method_name} failed: {e}") from e
        raise ArgusError(f"{method_name} failed: {last_err}") from last_err

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._client.close()

    # Async stubs (Phase 1+). Implement with httpx async if needed.
    async def analyze_folder_async(self, *args, **kwargs):
        """Async stub. Requires httpx async client."""
        raise NotImplementedError("Async support not implemented. Use sync version.")

    async def analyze_single_async(self, *args, **kwargs):
        raise NotImplementedError("Async support not implemented.")

    def analyze_folder(
        self,
        folder: Optional[str] = None,
        model: Optional[str] = None,
        limit: int = 20,
        write_sidecars: bool = False,
        sidecar_dir: Optional[str] = None,
        mise_gallery_id: Optional[int] = None,
        mise_project_id: Optional[int] = None,
        client_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST /analyze-folder. Returns the full result including run_id, photos, sidecars_written if enabled.
        Phase 3: mise_gallery_id / mise_project_id enable direct mise gallery import (server resolves via
        ARGUS_MISE_MEDIA_ROOT + mise layout if folder not provided).
        client_id triggers learned preferences application.
        """
        data = {
            "limit": str(limit),
            "write_sidecars": str(write_sidecars).lower(),
        }
        if folder:
            data["folder"] = folder
        if model:
            data["model"] = model
        if sidecar_dir:
            data["sidecar_dir"] = sidecar_dir
        if mise_gallery_id is not None:
            data["mise_gallery_id"] = str(mise_gallery_id)
        if mise_project_id is not None:
            data["mise_project_id"] = str(mise_project_id)
        effective_client_id = client_id or self.default_client_id
        if effective_client_id:
            data["client_id"] = effective_client_id

        url = f"{self.base_url}/analyze-folder"
        return self._request("post", url, data=data, method_name="analyze_folder")

    def import_mise_gallery(
        self,
        gallery_path: Optional[str] = None,
        mise_gallery_id: Optional[int] = None,
        mise_project_id: Optional[int] = None,
        limit: int = 20,
        write_sidecars: bool = False,
        sidecar_dir: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Convenience for direct mise import (Phase 3 slice 2).
        Delegates to analyze_folder with mise params. Supports resolution on server.
        """
        return self.analyze_folder(
            folder=gallery_path,
            model=model,
            limit=limit,
            write_sidecars=write_sidecars,
            sidecar_dir=sidecar_dir,
            mise_gallery_id=mise_gallery_id,
            mise_project_id=mise_project_id,
        )

    def import_mise_project(
        self,
        mise_project_id: int,
        gallery_path: Optional[str] = None,
        mise_gallery_id: Optional[int] = None,
        limit: int = 50,
        write_sidecars: bool = False,
        sidecar_dir: Optional[str] = None,
        model: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Phase 3 slice 3: submit batch 'entire project' job.
        Wires queue + sidecars + costs under the project concept.
        client_id for prefs.
        """
        data = {
            "mise_project_id": str(mise_project_id),
            "limit": str(limit),
            "write_sidecars": str(write_sidecars).lower(),
        }
        if gallery_path:
            data["gallery_path"] = gallery_path
        if mise_gallery_id is not None:
            data["mise_gallery_id"] = str(mise_gallery_id)
        if model:
            data["model"] = model
        if sidecar_dir:
            data["sidecar_dir"] = sidecar_dir
        effective_client_id = client_id or self.default_client_id
        if effective_client_id:
            data["client_id"] = effective_client_id
        url = f"{self.base_url}/import/mise-project"
        return self._request("post", url, data=data, method_name="import_mise_project")

    def get_preferences(self, client_id: Optional[str] = None, style: Optional[str] = None) -> dict:
        """GET /preferences"""
        qs = []
        if client_id:
            qs.append(f"client_id={client_id}")
        if style:
            qs.append(f"style={style}")
        url = f"{self.base_url}/preferences" + ("?" + "&".join(qs) if qs else "")
        return self._request("get", url, method_name="get_preferences")

    def set_preferences(self, client_id: str, prefs: dict, style: Optional[str] = None) -> dict:
        """POST /preferences"""
        data = {"client_id": client_id, "prefs": json.dumps(prefs)}
        if style:
            data["style"] = style
        return self._request("post", f"{self.base_url}/preferences", data=data, method_name="set_preferences")

    def analyze_single(
        self,
        path: Optional[str] = None,
        local_file: Optional[str] = None,
        model: Optional[str] = None,
        write_sidecar: bool = False,
        sidecar_dir: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST /analyze. Supports path (on server) or local_file (upload).
        Returns result + run_url + optional sidecar.
        """
        data = {
            "write_sidecar": str(write_sidecar).lower(),
        }
        if model:
            data["model"] = model
        if sidecar_dir:
            data["sidecar_dir"] = sidecar_dir
        effective_client_id = client_id or self.default_client_id
        if effective_client_id:
            data["client_id"] = effective_client_id

        url = f"{self.base_url}/analyze"
        if local_file:
            # upload local file (no automatic retry across open, wrap for consistency)
            try:
                with open(local_file, "rb") as f:
                    files = {"file": (Path(local_file).name, f, "image/jpeg")}
                    resp = self._client.post(url, data=data, files=files)
                    resp.raise_for_status()
                    return resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                raise ArgusError(f"analyze_single (upload) failed: {e}") from e
            except Exception as e:
                raise ArgusError(f"analyze_single (upload) failed: {e}") from e
        else:
            if path:
                data["path"] = path
            return self._request("post", url, data=data, method_name="analyze_single")

    def get_run(self, run_id: int) -> dict[str, Any]:
        """GET full structured run (uses /export)."""
        url = f"{self.base_url}/runs/{run_id}/export"
        return self._request("get", url, method_name="get_run")

    def get_photo_sidecar(self, run_id: int, photo_id: int) -> dict[str, Any]:
        """GET per-photo sidecar JSON."""
        url = f"{self.base_url}/runs/{run_id}/photo/{photo_id}/sidecar"
        return self._request("get", url, method_name="get_photo_sidecar")

    def fetch_and_write_sidecars(self, run_id: int, target_dir: str = ".") -> list[str]:
        """Fetch a run's photos and write .argus.json (and .iptc.json + .xmp if present) sidecars locally in target_dir.
        Phase 3 slice 5: XMP for LR/C1 compatibility.
        Useful when calling argus remotely (e.g. from flow) and you want the sidecars
        on the calling machine.
        """
        data = self.get_run(run_id)
        written = []
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        for photo in data.get("photos", []):
            basename = photo.get("basename", f"photo_{photo.get('id', 'unknown')}")
            # full argus sidecar
            sidecar = target / f"{basename}.argus.json"
            sidecar.write_text(json.dumps(photo, indent=2, ensure_ascii=False))
            written.append(str(sidecar))
            # iptc if present
            if photo.get("suggested_iptc"):
                iptc_sc = target / f"{basename}.iptc.json"
                iptc_sc.write_text(json.dumps(photo["suggested_iptc"], indent=2, ensure_ascii=False))
                written.append(str(iptc_sc))
                # XMP sidecar (LR compatible)
                xmp_sc = target / f"{basename}.xmp"
                xmp = self._build_xmp(photo)
                if xmp:
                    xmp_sc.write_text(xmp, encoding="utf-8")
                    written.append(str(xmp_sc))
        return written

    def _build_xmp(self, photo: dict) -> str:
        """Build XMP using the same exporter as the server."""
        return build_xmp(photo)

    def analyze_and_write_sidecars(self, folder: str, limit: int = 20, target_dir: str = ".", sidecar_dir: Optional[str] = None) -> dict:
        """Convenience: analyze folder (server may queue), then fetch and write sidecars locally.
        If queued, waits for completion (simple poll).
        """
        result = self.analyze_folder(folder, limit=limit, write_sidecars=True, sidecar_dir=sidecar_dir)
        if "job_id" in result:
            job_id = result["job_id"]
            result = self.poll_job(job_id, max_wait=300)
        run_id = result.get("run_id")
        if run_id:
            local_written = self.fetch_and_write_sidecars(run_id, target_dir=target_dir)
            result["local_sidecars_written"] = local_written
        return result

    def analyze_single_and_write_sidecars(self, path: str, target_dir: str = ".", sidecar_dir: Optional[str] = None) -> dict:
        """Convenience for single: analyze + pull sidecars locally."""
        result = self.analyze_single(path, write_sidecar=True, sidecar_dir=sidecar_dir)
        if "job_id" in result:
            result = self.poll_job(result["job_id"], max_wait=300)
        if "run_id" in result:
            local_written = self.fetch_and_write_sidecars(result["run_id"], target_dir=target_dir)
            result["local_sidecars_written"] = local_written
        return result

    def write_sidecars_for_run(self, run_id: int, sidecar_dir: Optional[str] = None) -> dict:
        """Call server to write sidecars for existing run. Returns list of written paths on server."""
        data = {"sidecar_dir": sidecar_dir} if sidecar_dir else {}
        return self._request("post", f"{self.base_url}/runs/{run_id}/write-sidecars", data=data, method_name="write_sidecars_for_run")

    def get_job(self, job_id: str) -> dict[str, Any]:
        """GET job status."""
        return self._request("get", f"{self.base_url}/jobs/{job_id}", method_name="get_job")

    def list_jobs(self, limit: int = 20) -> dict:
        """List recent jobs."""
        return self._request("get", f"{self.base_url}/jobs?limit={limit}", method_name="list_jobs")

    def get_costs(self, summary: bool = False) -> dict:
        """Phase 2: get simulated costs. summary=true for totals only."""
        url = f"{self.base_url}/jobs/costs"
        if summary:
            url += "?summary=true"
        return self._request("get", url, method_name="get_costs")

    def export_run_csv(self, run_id: int) -> str:
        """Download run as CSV (for batch mise/mnemosyne use)."""
        url = f"{self.base_url}/runs/{run_id}/export.csv"
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.text

    def poll_job(self, job_id: str, max_wait: int = 300, interval: float = 1.0) -> dict:
        """Poll a job until done/failed or timeout. Returns final job dict.
        Useful for long-running queued jobs.
        """
        for _ in range(int(max_wait / interval)):
            job = self.get_job(job_id)
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(interval)
        raise ArgusError(f"Job {job_id} did not complete within {max_wait}s")


if __name__ == "__main__":
    # quick smoke - uses mock on server, safe
    with ArgusClient(max_retries=2) as c:
        print("ArgusClient ready for tailnet use. Example base:", c.base_url)
        print("Retries enabled, context manager active.")
        # In real use with server running:
        # print(c.analyze_folder("data/demo", limit=1))  # would require server
    print("Done.")