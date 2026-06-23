"""Mnemosyne integration adapter for argus (Phase 3 slice 1).

This allows mnemosyne to optionally delegate its "look" step to argus over the tailnet
or locally, getting richer data but mapping to the {scene, hero_score} that mnemosyne
expects for now.

Usage in mnemosyne (example):
    from argus.app.mnemosyne_adapter import analyze_one
    # or with client
    from argus.app.client import ArgusClient, ArgusConfig
    config = ArgusConfig(base_url="http://mickey:8010", default_client_id="kevin")
    client = ArgusClient(config=config)
    result = analyze_one("/path/to/photo.jpg", client=client, client_id="kevin")

Or set ARGUS_URL in mnemosyne env and let its vision.py delegate (no argus import needed).

All with mock backend for safety.

Maps:
- scene = shot_type + first keyword or description snippet
- hero_score = culling.hero_potential
"""

from typing import Optional
from .client import ArgusClient


def analyze_one(image_path: str, client: Optional[ArgusClient] = None, is_local_file: bool = False, client_id: Optional[str] = None) -> dict:
    """Return mnemosyne-compatible {scene, hero_score} by calling argus.

    If is_local_file=True, uploads the local file to argus (for remote argus).
    Otherwise, assumes the path is accessible to the argus server.
    client_id for learned preferences.
    """
    if client is None:
        client = ArgusClient()

    kwargs = {"client_id": client_id} if client_id else {}
    if is_local_file:
        data = client.analyze_single(local_file=image_path, **kwargs)
    else:
        data = client.analyze_single(path=image_path, **kwargs)

    if "error" in data:
        # surface but don't crash caller; provide fallback
        return {"scene": "other", "hero_score": 0.5, "error": data["error"]}

    shot_type = data.get("shot_type", "other")
    keywords = data.get("keywords", [])
    # simple mapping for scene
    scene = f"{shot_type} {keywords[0] if keywords else ''}".strip()[:120]

    culling = data.get("culling", {})
    hero_score = float(culling.get("hero_potential", 0.5))

    return {
        "scene": scene,
        "hero_score": round(max(0.0, min(1.0, hero_score)), 2)
    }


def look_at_album(photos: list[dict], client: Optional[ArgusClient] = None, is_local_file: bool = False, client_id: Optional[str] = None) -> list[dict]:
    """Batch for mnemosyne-style: list of {'path': ..., } -> list of results with scene/hero.
    photos like mnemosyne's rows. Pass is_local_file=True to upload each (for remote argus).
    client_id for learned preferences (embedding).
    """
    if client is None:
        client = ArgusClient()
    results = []
    for p in photos:
        path = p["path"]
        res = analyze_one(path, client=client, is_local_file=is_local_file, client_id=client_id)
        results.append({**res, "path": path})
    return results
