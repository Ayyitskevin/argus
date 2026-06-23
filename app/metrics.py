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
    "jobs_retried": 0,
    "jobs_dead_letter": 0,
    "photos_analyzed": 0,
    "preferences_writes": 0,
    "photo_corrections": 0,
    "runs_archived": 0,
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


def prometheus_text() -> str:
    """Render counters in Prometheus exposition format (Phase 9)."""
    snap = snapshot()
    lines = [
        "# HELP argus_uptime_seconds Process uptime in seconds.",
        "# TYPE argus_uptime_seconds gauge",
        f"argus_uptime_seconds {snap['uptime_seconds']}",
    ]
    for name, value in sorted(snap["counters"].items()):
        metric = f"argus_{name}_total"
        lines.extend(
            [
                f"# HELP {metric} Argus counter {name}.",
                f"# TYPE {metric} counter",
                f"{metric} {value}",
            ]
        )
    return "\n".join(lines) + "\n"