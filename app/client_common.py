"""Shared request payload builders for sync/async Argus clients."""

from __future__ import annotations

from typing import Any, Optional


def build_analyze_folder_form(
    *,
    folder: Optional[str],
    model: Optional[str],
    limit: int,
    write_sidecars: bool,
    sidecar_dir: Optional[str],
    mise_gallery_id: Optional[int],
    mise_project_id: Optional[int],
    client_id: Optional[str],
    recursive: bool,
    callback_url: Optional[str],
) -> dict[str, str]:
    data: dict[str, str] = {
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
    if client_id:
        data["client_id"] = client_id
    if recursive:
        data["recursive"] = "true"
    if callback_url:
        data["callback_url"] = callback_url
    return data


def build_job_payload(
    *,
    folder: str,
    limit: int,
    write_sidecars: bool,
    sidecar_dir: Optional[str],
    client_id: Optional[str],
    callback_url: Optional[str],
    recursive: bool,
    model: Optional[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "folder": folder,
        "limit": limit,
        "write_sidecars": write_sidecars,
        "recursive": recursive,
    }
    if sidecar_dir:
        payload["sidecar_dir"] = sidecar_dir
    if client_id:
        payload["client_id"] = client_id
    if callback_url:
        payload["callback_url"] = callback_url
    if model:
        payload["model"] = model
    return payload