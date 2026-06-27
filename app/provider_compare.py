"""Grok ↔ Qwen parity harness — measure the vision cutover before flipping.

Pure, deterministic comparison of two analysis runs of the *same* gallery (e.g.
a Grok shadow run and a Qwen shadow run). Diffs the structured output, cost, and
latency so an operator (or Mise's /admin/vision-cutover) can decide whether the
local Qwen model is close enough to cut over — a measured, reversible move.

No model calls, no network: it consumes the persisted ``db.get_full_run`` shape
(``{"run": {...}, "photos": [...]}``) that both providers already write, and
reuses the structured-output serializer so the comparison is on exactly the
contract Mise validates.
"""
from __future__ import annotations

from typing import Any

from . import structured_output

# Default acceptance thresholds for the cutover verdict. A run pair is "within
# tolerance" when the providers' mean score divergence is small and they agree on
# keywords/shot_type often enough to be interchangeable on the validation gate.
DEFAULT_SCORE_TOLERANCE = 0.15
DEFAULT_KEYWORD_AGREEMENT = 0.40
DEFAULT_SHOT_TYPE_AGREEMENT = 0.60

_KNOWN_PROVIDERS = ("qwen", "grok", "mock", "openai", "anthropic")


def provider_of(run: dict) -> str:
    """Best-effort provider label for a run from its stored model (or explicit field)."""
    explicit = str(run.get("provider") or "").strip().lower()
    if explicit:
        return explicit
    model = str(run.get("model") or "").lower()
    for name in _KNOWN_PROVIDERS:  # qwen before grok so a hybrid name resolves to qwen
        if name in model:
            return name
    return model.split(":", 1)[0] or "unknown"


def _jaccard(a: list[str], b: list[str]) -> float:
    sa = {k.strip().lower() for k in a if k.strip()}
    sb = {k.strip().lower() for k in b if k.strip()}
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _abs_delta(x: float | None, y: float | None) -> float | None:
    if x is None or y is None:
        return None
    return round(abs(x - y), 4)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def compare_provider_runs(
    data_a: dict,
    data_b: dict,
    *,
    score_tolerance: float = DEFAULT_SCORE_TOLERANCE,
    min_keyword_agreement: float = DEFAULT_KEYWORD_AGREEMENT,
    min_shot_type_agreement: float = DEFAULT_SHOT_TYPE_AGREEMENT,
    per_photo_limit: int = 200,
) -> dict[str, Any]:
    """Provider-parity report between two full-run dicts (a=baseline, b=challenger).

    ``a`` is conventionally the incumbent (Grok) and ``b`` the challenger (Qwen),
    but the report is symmetric. Photos are matched by basename (the structured
    contract's key), so it tolerates differing stored paths."""
    photos_a = data_a.get("photos") or []
    photos_b = data_b.get("photos") or []

    norm_a = {p["basename"]: p for p in structured_output.photos_to_vision(photos_a)}
    norm_b = {p["basename"]: p for p in structured_output.photos_to_vision(photos_b)}
    shot_a = {
        structured_output._basename(p): str(p.get("shot_type") or "other") for p in photos_a
    }
    shot_b = {
        structured_output._basename(p): str(p.get("shot_type") or "other") for p in photos_b
    }

    common = sorted(set(norm_a) & set(norm_b))
    only_a = sorted(set(norm_a) - set(norm_b))
    only_b = sorted(set(norm_b) - set(norm_a))

    keeper_deltas: list[float] = []
    hero_deltas: list[float] = []
    jaccards: list[float] = []
    shot_agreements: list[int] = []
    per_photo: list[dict[str, Any]] = []

    for name in common:
        pa, pb = norm_a[name], norm_b[name]
        kd = _abs_delta(pa["keeper_score"], pb["keeper_score"])
        hd = _abs_delta(pa["hero_potential"], pb["hero_potential"])
        jac = _jaccard(pa["keywords"], pb["keywords"])
        sa, sb = shot_a.get(name, "other"), shot_b.get(name, "other")
        agree = int(sa == sb)
        if kd is not None:
            keeper_deltas.append(kd)
        if hd is not None:
            hero_deltas.append(hd)
        jaccards.append(jac)
        shot_agreements.append(agree)
        per_photo.append(
            {
                "basename": name,
                "keeper_a": pa["keeper_score"],
                "keeper_b": pb["keeper_score"],
                "keeper_abs_delta": kd,
                "hero_a": pa["hero_potential"],
                "hero_b": pb["hero_potential"],
                "hero_abs_delta": hd,
                "keyword_jaccard": round(jac, 4),
                "shot_type_a": sa,
                "shot_type_b": sb,
                "shot_type_agree": bool(agree),
            }
        )

    # Worst-divergence photos first so reviewers see the riskiest cases.
    per_photo.sort(key=lambda r: (r["keeper_abs_delta"] is None, -(r["keeper_abs_delta"] or 0.0)))

    cost_a, latency_a = structured_output.aggregate_cost_latency(photos_a)
    cost_b, latency_b = structured_output.aggregate_cost_latency(photos_b)

    mean_keeper = _mean(keeper_deltas)
    mean_hero = _mean(hero_deltas)
    mean_jac = _mean(jaccards)
    shot_rate = _mean([float(x) for x in shot_agreements])

    within = (
        bool(common)
        and (mean_keeper is None or mean_keeper <= score_tolerance)
        and (mean_hero is None or mean_hero <= score_tolerance)
        and (mean_jac is None or mean_jac >= min_keyword_agreement)
        and (shot_rate is None or shot_rate >= min_shot_type_agreement)
    )
    reasons: list[str] = []
    if not common:
        reasons.append("no overlapping photos to compare")
    if mean_keeper is not None and mean_keeper > score_tolerance:
        reasons.append(f"mean keeper Δ {mean_keeper} > {score_tolerance}")
    if mean_hero is not None and mean_hero > score_tolerance:
        reasons.append(f"mean hero Δ {mean_hero} > {score_tolerance}")
    if mean_jac is not None and mean_jac < min_keyword_agreement:
        reasons.append(f"keyword agreement {mean_jac} < {min_keyword_agreement}")
    if shot_rate is not None and shot_rate < min_shot_type_agreement:
        reasons.append(f"shot_type agreement {shot_rate} < {min_shot_type_agreement}")

    return {
        "providers": {"a": provider_of(data_a.get("run") or {}), "b": provider_of(data_b.get("run") or {})},
        "runs": {"a": (data_a.get("run") or {}).get("id"), "b": (data_b.get("run") or {}).get("id")},
        "models": {"a": (data_a.get("run") or {}).get("model"), "b": (data_b.get("run") or {}).get("model")},
        "photo_counts": {
            "a": len(photos_a),
            "b": len(photos_b),
            "common": len(common),
            "only_a": len(only_a),
            "only_b": len(only_b),
        },
        "cost_usd": {"a": cost_a, "b": cost_b, "delta": round(cost_b - cost_a, 6)},
        "latency_ms": {"a": latency_a, "b": latency_b, "delta": round(latency_b - latency_a, 1)},
        "agreement": {
            "mean_keeper_abs_delta": mean_keeper,
            "mean_hero_abs_delta": mean_hero,
            "keyword_jaccard_mean": mean_jac,
            "shot_type_agree_rate": shot_rate,
        },
        "verdict": {
            "within_tolerance": within,
            "thresholds": {
                "score_tolerance": score_tolerance,
                "min_keyword_agreement": min_keyword_agreement,
                "min_shot_type_agreement": min_shot_type_agreement,
            },
            "reasons": reasons,
        },
        "only_in_a": only_a[:per_photo_limit],
        "only_in_b": only_b[:per_photo_limit],
        "per_photo": per_photo[:per_photo_limit],
    }
