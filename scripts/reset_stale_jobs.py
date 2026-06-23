#!/usr/bin/env python3
"""Clean orphaned and ephemeral jobs from the Argus queue ledger.

Usage:
    python scripts/reset_stale_jobs.py --all          # recommended tailnet cleanup
    python scripts/reset_stale_jobs.py --stale-running
    python scripts/reset_stale_jobs.py --purge-ephemeral --purge-failed
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402


def _counts() -> dict[str, int]:
    return {
        "queued": db.count_jobs_by_status("queued"),
        "running": db.count_jobs_by_status("running"),
        "done": db.count_jobs_by_status("done"),
        "failed": db.count_jobs_by_status("failed"),
        "dead_letter": db.count_jobs_by_status("dead_letter"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset stale Argus jobs")
    parser.add_argument(
        "--stale-running",
        action="store_true",
        help="mark all running jobs failed (orphaned worker claims)",
    )
    parser.add_argument(
        "--purge-ephemeral",
        action="store_true",
        help="delete jobs whose folder path starts with /tmp/",
    )
    parser.add_argument(
        "--purge-failed",
        action="store_true",
        help="delete failed and dead_letter jobs",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="stale-running + purge-ephemeral + purge-failed",
    )
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=None,
        help="only reconcile running jobs older than N minutes (default: all running)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print counts only")
    args = parser.parse_args()

    if args.all:
        args.stale_running = True
        args.purge_ephemeral = True
        args.purge_failed = True

    if not (args.stale_running or args.purge_ephemeral or args.purge_failed):
        parser.error("specify --all or at least one cleanup action")

    db.init()
    before = _counts()
    print(f"DB: {config.DB_PATH}")
    print(f"Before: {before}")

    if args.dry_run:
        print("Dry run — no changes made.")
        return 0

    reconciled = 0
    purged_ephemeral = 0
    purged_failed = 0

    if args.stale_running:
        reconciled = db.reconcile_stale_running_jobs(max_age_minutes=args.max_age_minutes)
        print(f"Reconciled stale running → failed: {reconciled}")

    if args.purge_ephemeral:
        purged_ephemeral = db.purge_jobs(folder_prefixes=("/tmp/",))
        print(f"Purged ephemeral /tmp jobs: {purged_ephemeral}")

    if args.purge_failed:
        purged_failed = db.purge_jobs(statuses=("failed", "dead_letter"))
        print(f"Purged failed/dead_letter jobs: {purged_failed}")

    after = _counts()
    print(f"After:  {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())