#!/usr/bin/env python3
"""Dogfood a real Lightroom/C1 export folder with Grok vision.

Usage:
    python scripts/dogfood_export_folder.py /path/to/export --limit 10
    python scripts/dogfood_export_folder.py /path/to/export --client-id kevin-fb

Writes JSON report under data/ and prints review UI link.
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
os.environ.setdefault("ARGUS_VISION_BACKEND", "grok")

from app import config  # noqa: E402
from app.service import perform_folder_analyze  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus export-folder dogfood")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--client-id", default="export-dogfood")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--sync", action="store_true", help="wait for queued job (poll DB)")
    args = parser.parse_args()

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 1
    if config.VISION_BACKEND != "grok":
        print("ARGUS_VISION_BACKEND must be grok", file=sys.stderr)
        return 1
    if not config.XAI_API_KEY:
        print("XAI_API_KEY required", file=sys.stderr)
        return 1

    n_jpg = len(list(folder.rglob("*.jpg") if args.recursive else folder.glob("*.jpg")))
    print(f"Folder: {folder}")
    print(f"JPEGs visible: {n_jpg} · limit={args.limit} · backend={config.VISION_BACKEND}")

    result = perform_folder_analyze(
        folder=str(folder),
        limit=args.limit,
        client_id=args.client_id,
        recursive=args.recursive,
        skip_dedup=True,
    )

    run_id = result.get("run_id")
    job_id = result.get("job_id")

    if result.get("mode") == "queued" and args.sync and job_id:
        from app import db

        db.init()
        deadline = time.time() + 600
        while time.time() < deadline:
            job = db.get_job(job_id, tenant_id=db.GLOBAL_SCOPE)
            if job and job.get("status") == "done":
                run_id = job.get("run_id")
                break
            if job and job.get("status") in {"failed", "dead_letter"}:
                print(f"Job failed: {job.get('error')}", file=sys.stderr)
                return 2
            time.sleep(2)

    report = {
        "folder": str(folder),
        "limit": args.limit,
        "client_id": args.client_id,
        "run_id": run_id,
        "job_id": job_id,
        "mode": result.get("mode"),
    }
    out = config.DATA_DIR / f"dogfood-export-{int(time.time())}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if run_id:
        print(f"\nReview: http://127.0.0.1:{config.PORT}/runs/{run_id}")
        print(f"Cull with j/k move · s save · r reject · p pick · b boost keyword")
    elif job_id:
        print(f"\nQueued job {job_id} — http://127.0.0.1:{config.PORT}/ui/jobs/{job_id}")
    print(f"Report: {out}")
    return 0 if run_id or job_id else 2


if __name__ == "__main__":
    raise SystemExit(main())