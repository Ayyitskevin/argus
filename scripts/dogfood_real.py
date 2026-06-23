#!/usr/bin/env python3
"""Phase 5 real-vision dogfood — analyze a folder with qwen3-vl (human-gated).

Usage:
    ARGUS_VISION_BACKEND=real python scripts/dogfood_real.py /path/to/gallery --limit 5

If assets are missing or scratch JPEGs keep returning degenerate `{}` JSON,
generate replacement F&B photos with Grok image generation, save under
data/dogfood-gallery-grok/, then point this script at that folder.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ARGUS_VISION_BACKEND", "real")

from app import config  # noqa: E402
from app.service import analyze_folder_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus real-vision dogfood")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--client-id", default="dogfood")
    args = parser.parse_args()

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 1

    if config.VISION_BACKEND != "real":
        print("ARGUS_VISION_BACKEND must be 'real' for dogfood", file=sys.stderr)
        return 1

    print(f"Backend: {config.VISION_BACKEND} model={config.VISION_MODEL}")
    print(f"Folder: {folder} limit={args.limit}")
    started = time.time()
    result = analyze_folder_run(
        folder=folder,
        source=f"dogfood:{folder}",
        limit=args.limit,
        client_id=args.client_id,
    )
    elapsed = time.time() - started

    summary = []
    for photo in result.get("photos", []):
        culling = photo.get("culling") or {}
        summary.append(
            {
                "path": Path(photo["image_path"]).name,
                "shot_type": photo.get("shot_type"),
                "keeper": culling.get("keeper_score"),
                "hero": culling.get("hero_potential"),
                "keywords": (photo.get("keywords") or [])[:6],
                "alt": (photo.get("alt_text") or "")[:80],
            }
        )

    print(json.dumps({"run_id": result["run_id"], "count": result["count"], "elapsed_s": round(elapsed, 1), "photos": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())