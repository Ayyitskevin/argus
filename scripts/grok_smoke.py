#!/usr/bin/env python3
"""One-image Grok vision smoke test — verifies API key and credits.

Usage:
    ARGUS_VISION_BACKEND=grok python scripts/grok_smoke.py [image.jpg]

Exits 0 on success, 1 on config error, 2 on API/credit failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ARGUS_VISION_BACKEND", "grok")

from app import config  # noqa: E402
from app.grok_client import GrokVisionError  # noqa: E402
from app.vision import analyze_image  # noqa: E402


def _default_image() -> Path:
    candidates = [
        ROOT / "data" / "demo" / "01-appetite.jpg",
        Path("/home/kevin-lee/ai-workspace/argus/data/demo/01-appetite.jpg"),
        Path("/home/kevin-lee/ai-workspace/argus/data/dogfood-gallery-grok/01-hero-plate.jpg"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("no sample image — pass a path or add data/demo/01-appetite.jpg")


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus Grok vision smoke test")
    parser.add_argument("image", nargs="?", type=Path, help="JPEG/PNG to analyze")
    parser.add_argument("--model", default=None, help="override ARGUS_VISION_MODEL")
    args = parser.parse_args()

    if config.VISION_BACKEND != "grok":
        print("Set ARGUS_VISION_BACKEND=grok", file=sys.stderr)
        return 1
    if not config.XAI_API_KEY:
        print("XAI_API_KEY is not set — add to .env", file=sys.stderr)
        return 1

    try:
        image_path = (args.image or _default_image()).expanduser().resolve()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not image_path.is_file():
        print(f"Image not found: {image_path}", file=sys.stderr)
        return 1

    model = args.model or config.VISION_MODEL
    print(f"Smoke: backend=grok model={model} image={image_path.name}", flush=True)

    try:
        result = analyze_image(image_path, model=model)
    except GrokVisionError as exc:
        print(f"Grok API error: {exc}", file=sys.stderr)
        if "credit" in str(exc).lower() or "permission" in str(exc).lower():
            print("→ Add credits at https://console.x.ai", file=sys.stderr)
        return 2

    if result.keywords == ["analysis-failed"]:
        print(f"Analysis failed: {result.culling.notes}", file=sys.stderr)
        return 2

    payload = {
        "image": str(image_path),
        "model": result.model,
        "shot_type": result.shot_type,
        "keeper_score": result.culling.keeper_score,
        "hero_potential": result.culling.hero_potential,
        "keywords": result.keywords[:5],
        "alt_text": result.alt_text[:120],
    }
    print(json.dumps(payload, indent=2))
    print("OK — Grok vision is working.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())