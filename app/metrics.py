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
    "jobs_recovered": 0,
    "jobs_dead_letter": 0,
    "photos_analyzed": 0,
    "preferences_writes": 0,
    "photo_corrections": 0,
    "runs_archived": 0,
    "grok_api_calls": 0,
    "grok_prompt_tokens": 0,
    "grok_completion_tokens": 0,
    "grok_total_tokens": 0,
}
_gauges: dict[str, float] = {
    "grok_cost_usd": 0.0,
}
_tenant_counters: dict[str, dict[str, int]] = {}


def inc(name: str, amount: int = 1) -> None:
    with _lock:
        _counters[name] = _counters.get(name, 0) + amount


def inc_tenant(tenant_id: str | None, name: str, amount: int = 1) -> None:
    if not tenant_id:
        return
    with _lock:
        bucket = _tenant_counters.setdefault(tenant_id, {})
        bucket[name] = bucket.get(name, 0) + amount


def add_float(name: str, amount: float) -> None:
    with _lock:
        _gauges[name] = _gauges.get(name, 0.0) + amount


def record_grok_usage(usage: dict) -> None:
    """Increment Grok API counters from a parse_usage() dict."""
    inc("grok_api_calls")
    inc("grok_prompt_tokens", int(usage.get("prompt_tokens") or 0))
    inc("grok_completion_tokens", int(usage.get("completion_tokens") or 0))
    inc("grok_total_tokens", int(usage.get("total_tokens") or 0))
    cost = usage.get("cost_usd")
    if cost is not None:
        add_float("grok_cost_usd", float(cost))


def snapshot(*, tenant_id: str | None = None) -> dict:
    with _lock:
        out = {
            "uptime_seconds": round(time.time() - _started_at, 1),
            "counters": dict(_counters),
            "gauges": {k: round(v, 6) for k, v in _gauges.items()},
        }
        if tenant_id:
            out["tenant_counters"] = dict(_tenant_counters.get(tenant_id, {}))
        elif _tenant_counters:
            out["by_tenant"] = {tid: dict(vals) for tid, vals in _tenant_counters.items()}
        return out


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
    for name, value in sorted(snap["gauges"].items()):
        metric = f"argus_{name}"
        lines.extend(
            [
                f"# HELP {metric} Argus gauge {name}.",
                f"# TYPE {metric} gauge",
                f"{metric} {value}",
            ]
        )
    for tenant_id, counters in sorted(snap.get("by_tenant", {}).items()):
        for name, value in sorted(counters.items()):
            metric = f"argus_tenant_{name}_total"
            lines.extend(
                [
                    f"# HELP {metric} Per-tenant counter {name}.",
                    f"# TYPE {metric} counter",
                    f'{metric}{{tenant_id="{tenant_id}"}} {value}',
                ]
            )
    return "\n".join(lines) + "\n"