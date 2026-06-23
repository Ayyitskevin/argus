#!/usr/bin/env python3
"""Prove prefs corrections flow into the next analyze (Phase 7 gate).

Usage:
    ARGUS_VISION_BACKEND=mock python scripts/dogfood_prefs_roundtrip.py path/to.jpg
    ARGUS_VISION_BACKEND=grok python scripts/dogfood_prefs_roundtrip.py data/demo/01-appetite.jpg

Exit 0 when promoted keyword appears in second analyze.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, service  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Prefs roundtrip dogfood")
    parser.add_argument("image", type=Path)
    parser.add_argument("--client-id", default="prefs-roundtrip")
    parser.add_argument("--boost", default="heritage-heirloom")
    args = parser.parse_args()

    path = args.image.expanduser().resolve()
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 1

    db.init()
    client_id = args.client_id
    boost = args.boost.strip()

    first = service.analyze_single_image(image_path=path, client_id=client_id)
    run_a = int(first["run_id"])
    full_a = db.get_full_run(run_a, tenant_id=db.GLOBAL_SCOPE) or {}
    photo_id = int((full_a.get("photos") or [{}])[0]["id"])

    service.apply_photo_correction(
        run_a,
        photo_id,
        promote_keywords=[boost],
    )
    prefs = db.get_preferences(client_id, tenant_id=db.GLOBAL_SCOPE) or {}
    boosts = prefs.get("keyword_boosts") or []
    if boost not in boosts:
        print(f"FAIL: {boost!r} not in keyword_boosts {boosts}", file=sys.stderr)
        return 2

    second = service.analyze_single_image(image_path=path, client_id=client_id)
    full_b = db.get_full_run(int(second["run_id"]), tenant_id=db.GLOBAL_SCOPE) or {}
    keywords = (full_b.get("photos") or [{}])[0].get("keywords") or []
    if boost not in keywords:
        print(f"FAIL: boosted keyword not in second analyze: {keywords}", file=sys.stderr)
        return 2

    print(f"PASS: {boost!r} promoted and present after re-analyze")
    print(f"  run_a={run_a} run_b={second['run_id']} keywords={keywords[:6]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())