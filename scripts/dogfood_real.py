#!/usr/bin/env python3
"""Real-vision dogfood — analyze a folder with xAI Grok (human-gated).

Usage:
    ARGUS_VISION_BACKEND=grok python scripts/dogfood_real.py /path/to/gallery --limit 5

If assets are missing or scratch JPEGs keep returning degenerate `{}` JSON,
generate replacement F&B photos with Grok image generation, save under
data/dogfood-gallery-grok/, then point this script at that folder.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ARGUS_VISION_BACKEND", "grok")

from app import config  # noqa: E402
from app.service import analyze_folder_estimate, analyze_folder_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus real-vision dogfood")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=5, help="0 = entire folder")
    parser.add_argument("--client-id", default="dogfood")
    parser.add_argument("--style", default=None, help="f_and_b, events, or portrait")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="override ARGUS_DATA_DIR for isolated dogfood DB",
    )
    args = parser.parse_args()

    if args.data_dir:
        os.environ["ARGUS_DATA_DIR"] = args.data_dir

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 1

    if config.VISION_BACKEND != "grok":
        print("ARGUS_VISION_BACKEND must be 'grok' (or 'real') for dogfood", file=sys.stderr)
        return 1
    if not config.XAI_API_KEY:
        print("XAI_API_KEY must be set for Grok vision", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"Backend: {config.VISION_BACKEND} model={config.VISION_MODEL}", flush=True)
    effective_limit = None if args.limit <= 0 else args.limit
    estimate = analyze_folder_estimate(folder, limit=effective_limit, recursive=args.recursive)
    print(f"Folder: {folder} limit={args.limit} recursive={args.recursive}", flush=True)
    if estimate.get("estimated_cost_usd") is not None:
        print(
            f"Estimate: {estimate.get('image_count')} images, "
            f"~${estimate['estimated_cost_usd']:.2f}",
            flush=True,
        )
    if args.style:
        print(f"Style: {args.style}", flush=True)
    started = time.time()
    result = analyze_folder_run(
        folder=folder,
        source=f"client:{args.client_id}|dogfood:{folder}",
        limit=effective_limit,
        client_id=args.client_id,
        recursive=args.recursive,
        style=args.style,
    )
    elapsed = time.time() - started

    summary = []
    degenerate = 0
    for photo in result.get("photos", []):
        culling = photo.get("culling") or {}
        keywords = photo.get("keywords") or []
        if not keywords or keywords == ["analysis-failed"]:
            degenerate += 1
        summary.append(
            {
                "path": Path(photo["image_path"]).name,
                "shot_type": photo.get("shot_type"),
                "keeper": culling.get("keeper_score"),
                "hero": culling.get("hero_potential"),
                "keywords": keywords[:6],
                "alt": (photo.get("alt_text") or "")[:80],
                "notes": (culling.get("notes") or "")[:120],
            }
        )

    payload = {
        "run_id": result["run_id"],
        "count": result["count"],
        "elapsed_s": round(elapsed, 1),
        "degenerate": degenerate,
        "degenerate_rate": round(degenerate / max(result["count"], 1), 3),
        "run_url": f"/runs/{result['run_id']}",
        "photos": summary,
    }
    print(json.dumps(payload, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())