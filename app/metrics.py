"""Lightweight in-process counters for Argus (Phase 4).

No external deps — suitable for local/tailnet dogfooding. Counters reset on
process restart (acceptable for this service tier).
"""
from __future__ import annotations

import time
from threading import Lock

_lock = Lock()
_started_at = time.time()
_counters: dict[str, int] = {
    "analyze_single": 0,
    "analyze_folder": 0,
    "jobs_completed": 0,
    "jobs_failed": 0,
    "photos_analyzed": 0,
    "preferences_writes": 0,
}


def inc(name: str, amount: int = 1) -> None:
    with _lock:
        _counters[name] = _counters.get(name, 0) + amount


def snapshot() -> dict:
    with _lock:
        return {
            "uptime_seconds": round(time.time() - _started_at, 1),
            "counters": dict(_counters),
        }