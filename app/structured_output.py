"""Structured-output mode for the Mise vision cutover.

Pure serialization layer that maps Argus's internal per-photo analysis onto the
shared ``schemas/vision.schema.json`` contract so the Mise validation gate can
compare Argus (Grok) against the Qwen3-VL challenger apples-to-apples.

Nothing here calls a model or the network — it only reshapes data that
``vision.analyze_*`` already produced. The live Grok export/callback path is
untouched; this is only consulted when ``config.STRUCTURED_OUTPUT_ENABLED``.

Shape (per photo): ``{basename, keywords, alt_text, keeper_score, hero_potential}``.
Callback body adds run-level ``cost_usd`` + ``latency_ms`` and echoes the Mise
``correlation_id`` so shadow pairs link. Output is a deterministic function of a
persisted run, so a retry re-emits an identical payload (idempotent).
"""
from __future__ import annotations

import os
from typing import Any

from . import config

MICRO_PER_USD = 1_000_000

# Status semantics Mise records as the run's last state. A callback always carries
# exactly one of these so Mise's status is never ambiguous.
CALLBACK_STATUSES = frozenset({"queued", "done", "error"})


def idempotency_key(gallery_id: int, run_id: int) -> str:
    """Stable dedupe key for a (gallery, run) callback.

    Identical across retries and re-deliveries (same run → same key), and across
    re-analyses of an unchanged gallery (the mise_dedup run cache returns the same
    run_id). A *changed* gallery yields a new run → new run_id → new key, i.e. a
    genuinely new logical result. Mise no-ops a key it has already applied;
    Argus's own dead-letter/re-delivery store is keyed on it too."""
    return f"argus-g{int(gallery_id)}-r{int(run_id)}"


def normalize_status(status: str | None) -> str:
    """Coerce to one of the contract statuses (queued|done|error)."""
    value = (status or "done").strip().lower()
    return value if value in CALLBACK_STATUSES else "done"


def _clamp_score(value: Any) -> float | None:
    """Coerce a culling score into a [0,1] float, or None when absent/unusable.

    Mise rejects out-of-range scores deterministically, so we clamp rather than
    forward a bad value — a clamped score still validates; a None still validates.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score:  # NaN
        return None
    return max(0.0, min(1.0, round(score, 6)))


def _one_line_alt(value: Any) -> str | None:
    """Collapse alt text to a single trimmed line, or None when empty."""
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def _keywords(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(k).strip() for k in value if str(k).strip()]


def _basename(photo: dict) -> str:
    name = photo.get("basename")
    if name:
        return str(name)
    return os.path.basename(str(photo.get("image_path") or ""))


def photo_to_vision(photo: dict) -> dict[str, Any]:
    """Map one internal photo dict to the vision.schema.json per-photo shape.

    Accepts both the in-memory ``result_to_dict`` shape (``culling`` dict,
    ``cost_usd``) and the persisted ``db.get_full_run`` shape (``culling`` dict,
    ``cost_micro_usd``). Scores live under ``culling`` in both.
    """
    culling = photo.get("culling")
    if not isinstance(culling, dict):
        culling = {}
    return {
        "basename": _basename(photo),
        "keywords": _keywords(photo.get("keywords")),
        "alt_text": _one_line_alt(photo.get("alt_text")),
        "keeper_score": _clamp_score(culling.get("keeper_score")),
        "hero_potential": _clamp_score(culling.get("hero_potential")),
    }


def photos_to_vision(photos: list[dict]) -> list[dict[str, Any]]:
    """Serialize a list of internal photos, dropping any without a basename
    (Mise matches assets by basename, so a nameless row is unusable)."""
    out: list[dict[str, Any]] = []
    for photo in photos or []:
        mapped = photo_to_vision(photo)
        if mapped["basename"]:
            out.append(mapped)
    return out


def run_to_vision(full_run: dict) -> dict[str, Any]:
    """Schema-conforming ``{"photos": [...]}`` for a ``db.get_full_run`` result."""
    return {"photos": photos_to_vision(full_run.get("photos") or [])}


def _photo_cost_usd(photo: dict) -> float:
    cost = photo.get("cost_usd")
    if cost is not None:
        try:
            return float(cost)
        except (TypeError, ValueError):
            return 0.0
    micros = photo.get("cost_micro_usd")
    if micros is not None:
        try:
            return int(micros) / MICRO_PER_USD
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def aggregate_cost_latency(photos: list[dict]) -> tuple[float, float]:
    """Sum per-photo spend and inference latency for a run.

    Summed (not wall-clock) so the figures are deterministic and reproduce
    exactly on an idempotent re-emit, regardless of analyze parallelism.
    """
    cost_usd = 0.0
    latency_ms = 0.0
    for photo in photos or []:
        cost_usd += _photo_cost_usd(photo)
        latency = photo.get("latency_ms")
        if latency is not None:
            try:
                latency_ms += float(latency)
            except (TypeError, ValueError):
                pass
    return round(cost_usd, 6), round(latency_ms, 1)


def build_callback_payload(
    full_run: dict,
    *,
    gallery_id: int,
    run_id: int,
    correlation_id: str | None = None,
    status: str = "done",
    provider: str | None = None,
) -> dict[str, Any]:
    """Build the body for POST {MISE_URL}/api/argus/callback?gallery_id=<id>.

    Includes the schema ``photos`` array plus the run-level ``cost_usd`` and
    ``latency_ms`` that Mise's ai_runs ledger + /admin/ai-cost report consume,
    and echoes ``correlation_id`` so Mise can pair Argus and Qwen shadow rows.
    """
    photos = full_run.get("photos") or []
    cost_usd, latency_ms = aggregate_cost_latency(photos)
    payload: dict[str, Any] = {
        "schema": "vision.schema.json",
        "provider": provider or config.STRUCTURED_PROVIDER,
        "gallery_id": gallery_id,
        "run_id": run_id,
        # Stable per (gallery, run) so Mise and Argus dedupe re-deliveries/retries.
        "idempotency_key": idempotency_key(gallery_id, run_id),
        "status": normalize_status(status),
        "photos": photos_to_vision(photos),
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
    }
    if correlation_id:
        payload["correlation_id"] = correlation_id
    return payload
