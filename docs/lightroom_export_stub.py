#!/usr/bin/env python3
"""Lightroom / Capture One export helper — calls Argus over tailnet, writes sidecars locally.

Usage (mock-safe):
    ARGUS_VISION_BACKEND=mock python docs/lightroom_export_stub.py /path/to/gallery \\
        --base-url http://mickey:8010 --client-id kevin --limit 0

Queue mode (recommended for large galleries):
    python docs/lightroom_export_stub.py /path/to/gallery --queue --max-wait 7200

If the server has ARGUS_API_TOKEN set, pass --token or export ARGUS_API_TOKEN.
"""
from __future__ import annotations

import argparse
import json
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
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="max photos (0 = entire folder)",
    )
    parser.add_argument("--target-dir", default=".", help="Where to write pulled sidecars")
    parser.add_argument("--token", default=os.environ.get("ARGUS_API_TOKEN"))
    parser.add_argument("--recursive", action="store_true", help="scan subfolders")
    parser.add_argument("--manifest-out", default=None, help="write manifest.json locally after run")
    parser.add_argument(
        "--queue",
        action="store_true",
        help="submit via POST /jobs (preferred when ARGUS_QUEUE_ENABLED)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="return after queue submit without polling",
    )
    parser.add_argument(
        "--max-wait",
        type=int,
        default=7200,
        help="seconds to poll a queued job (default 7200)",
    )
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

    limit_label = "all" if args.limit <= 0 else str(args.limit)
    mode = "queue" if args.queue else "analyze-folder"
    print(
        f"Analyzing {folder} (limit={limit_label}, recursive={args.recursive}) "
        f"via {args.base_url} [{mode}] ..."
    )

    if args.queue:
        queued = http.create_job(
            str(folder),
            limit=args.limit,
            write_sidecars=True,
            sidecar_dir=str(folder),
            client_id=args.client_id,
            recursive=args.recursive,
        )
    else:
        queued = http.analyze_folder(
            str(folder),
            limit=args.limit,
            write_sidecars=True,
            sidecar_dir=str(folder),
            recursive=args.recursive,
        )

    job_id = queued.get("job_id")
    if job_id and not args.no_wait:
        queued = http.poll_job(job_id, max_wait=args.max_wait)
        result = queued.get("result") or {}
        run_id = result.get("run_id") or queued.get("run_id")
        count = result.get("count", 0)
    elif job_id and args.no_wait:
        print(f"Queued job {job_id} — not waiting (--no-wait)")
        return 0
    else:
        run_id = queued.get("run_id")
        count = queued.get("count", 0)
        result = queued

    local = []
    if run_id:
        local = http.fetch_and_write_sidecars(run_id, target_dir=args.target_dir)
        if args.manifest_out:
            manifest = http.get_run_manifest(run_id)
            out = Path(args.manifest_out).expanduser()
            out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            print(f"Manifest written to {out}")

    print(f"Run {run_id}: {count} photos")
    print(f"Wrote {len(local)} local sidecar file(s) under {args.target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())