#!/usr/bin/env python3
"""Lightroom / Capture One export helper — calls Argus over tailnet, writes sidecars locally.

Usage (mock-safe):
    ARGUS_VISION_BACKEND=mock python docs/lightroom_export_stub.py /path/to/gallery \\
        --base-url http://mickey:8010 --client-id kevin --limit 10

If the server has ARGUS_API_TOKEN set, pass --token or export ARGUS_API_TOKEN.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.client import ArgusClient, ArgusConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus Lightroom export stub")
    parser.add_argument("folder", help="Gallery folder with originals")
    parser.add_argument("--base-url", default=os.environ.get("ARGUS_BASE_URL", "http://127.0.0.1:8010"))
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--target-dir", default=".", help="Where to write pulled sidecars")
    parser.add_argument("--token", default=os.environ.get("ARGUS_API_TOKEN"))
    args = parser.parse_args()

    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    cfg = ArgusConfig(base_url=args.base_url, default_client_id=args.client_id)
    http = ArgusClient(config=cfg)
    if headers:
        http._client.headers.update(headers)

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        print(f"Folder not found: {folder}", file=sys.stderr)
        return 1

    print(f"Analyzing {folder} (limit={args.limit}) via {args.base_url} ...")
    result = http.analyze_and_write_sidecars(
        str(folder),
        limit=args.limit,
        target_dir=args.target_dir,
    )
    run_id = result.get("run_id")
    local = result.get("local_sidecars_written", [])
    print(f"Run {run_id}: {result.get('count', 0)} photos")
    print(f"Wrote {len(local)} local sidecar file(s) under {args.target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())