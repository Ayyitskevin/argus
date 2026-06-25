"""Mise gallery analyze dedup ledger (Phase 6 slice 2).

Argus owns the ledger — one active queued/done entry per gallery (+ optional client_id)
so Mise publish hooks and job retries cannot double-queue the same folder analyze.
"""

from __future__ import annotations

from typing import Any

from . import config, db

_ACTIVE = frozenset({"queued", "done"})


def dedup_key(mise_gallery_id: int, client_id: str | None = None) -> str:
    return db.mise_dedup_key(mise_gallery_id, client_id)


def lookup(mise_gallery_id: int, client_id: str | None = None) -> dict[str, Any] | None:
    """Return an existing analyze response shape when a queued/done entry exists."""
    row = db.get_mise_analyze_ledger(dedup_key(mise_gallery_id, client_id))
    if not row or row["status"] not in _ACTIVE:
        return None
    out: dict[str, Any] = {"deduped": True, "mise": {"gallery_id": mise_gallery_id}}
    if client_id:
        out["client_id"] = client_id
    if row["job_id"] and row["status"] == "queued":
        base = config.PUBLIC_URL.rstrip("/")
        return {
            **out,
            "mode": "queued",
            "job_id": row["job_id"],
            "status": "queued",
            "review_url": f"{base}/ui/jobs/{row['job_id']}",
        }
    if row["run_id"]:
        base = config.PUBLIC_URL.rstrip("/")
        return {
            **out,
            "mode": "sync",
            "run_id": row["run_id"],
            "status": "done",
            "review_url": f"{base}/runs/{row['run_id']}",
        }
    return None


def record_queued(mise_gallery_id: int, client_id: str | None, job_id: str) -> None:
    db.upsert_mise_analyze_ledger(
        dedup_key=dedup_key(mise_gallery_id, client_id),
        mise_gallery_id=mise_gallery_id,
        client_id=client_id,
        status="queued",
        job_id=job_id,
    )


def record_done(mise_gallery_id: int, client_id: str | None, run_id: int) -> None:
    db.upsert_mise_analyze_ledger(
        dedup_key=dedup_key(mise_gallery_id, client_id),
        mise_gallery_id=mise_gallery_id,
        client_id=client_id,
        status="done",
        run_id=run_id,
    )


def record_failed(mise_gallery_id: int, client_id: str | None) -> None:
    db.upsert_mise_analyze_ledger(
        dedup_key=dedup_key(mise_gallery_id, client_id),
        mise_gallery_id=mise_gallery_id,
        client_id=client_id,
        status="failed",
    )


def parse_mise_gallery_id(source: str | None) -> int | None:
    if not source or "gallery_id=" not in source:
        return None
    for part in source.split("|")[0].replace("mise:", "").split(","):
        if part.startswith("gallery_id="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return None
    return None