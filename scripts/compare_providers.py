#!/usr/bin/env python3
"""Grok ↔ Qwen parity harness — measure the vision cutover before flipping.

Two modes:

  # Compare two runs that already exist (e.g. a Mise shadow pair). No model calls.
  python scripts/compare_providers.py --run-a 41 --run-b 42

  # Run a folder through BOTH providers live, then diff (operator measurement).
  # Needs XAI_API_KEY for grok and a reachable ARGUS_QWEN_BASE_URL for qwen.
  python scripts/compare_providers.py --folder /path/to/gallery --limit 20

  # Plumbing self-test with the mock backend (no credits, no endpoint):
  python scripts/compare_providers.py --folder /path/to/gallery --mock

Prints a summary table and the verdict; --json writes the full report.
Exit 0 = within tolerance, 2 = diverged, 1 = usage/config error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _run_one_provider(folder: str, provider: str, *, limit: int | None, mock: bool):
    """Analyze a folder under a single provider and return a full-run-shaped dict."""
    from app import config, service, vision

    config.VISION_BACKEND = "mock" if mock else "grok"
    config.VISION_PROVIDER = provider
    results = vision.analyze_folder(folder, limit=limit)
    model = "mock" if mock else (config.QWEN_VISION_MODEL if provider == "qwen" else config.VISION_MODEL)
    return {
        "run": {"id": None, "model": f"{provider}:{model}"},
        "photos": [service.result_to_dict(r) for r in results],
    }


def _load_run(run_id: int) -> dict | None:
    from app import db

    return db.get_full_run(run_id, tenant_id=db.GLOBAL_SCOPE)


def _print_summary(report: dict) -> None:
    p = report["providers"]
    counts = report["photo_counts"]
    agree = report["agreement"]
    cost, lat = report["cost_usd"], report["latency_ms"]
    print(f"\n  provider A (baseline)  : {p['a']}  ({report['models']['a']})")
    print(f"  provider B (challenger): {p['b']}  ({report['models']['b']})")
    print(f"  photos: a={counts['a']} b={counts['b']} common={counts['common']} "
          f"only_a={counts['only_a']} only_b={counts['only_b']}")
    print(f"  cost_usd : a={cost['a']} b={cost['b']} (Δ {cost['delta']:+})")
    print(f"  latency_ms: a={lat['a']} b={lat['b']} (Δ {lat['delta']:+})")
    print(f"  mean |keeper Δ| : {agree['mean_keeper_abs_delta']}")
    print(f"  mean |hero Δ|   : {agree['mean_hero_abs_delta']}")
    print(f"  keyword agree   : {agree['keyword_jaccard_mean']}")
    print(f"  shot_type agree : {agree['shot_type_agree_rate']}")
    worst = report["per_photo"][:5]
    if worst:
        print("  worst keeper divergence:")
        for r in worst:
            print(f"    {r['basename']:<28} a={r['keeper_a']} b={r['keeper_b']} "
                  f"|Δ|={r['keeper_abs_delta']} kw={r['keyword_jaccard']}")
    verdict = report["verdict"]
    flag = "WITHIN TOLERANCE" if verdict["within_tolerance"] else "DIVERGED"
    print(f"\n  verdict: {flag}")
    for reason in verdict["reasons"]:
        print(f"    - {reason}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Grok↔Qwen vision parity harness")
    ap.add_argument("--run-a", type=int, help="baseline run id (e.g. Grok)")
    ap.add_argument("--run-b", type=int, help="challenger run id (e.g. Qwen)")
    ap.add_argument("--folder", help="run this folder through both providers live, then diff")
    ap.add_argument("--limit", type=int, default=0, help="max images (0 = all)")
    ap.add_argument("--mock", action="store_true", help="use the mock backend (plumbing self-test)")
    ap.add_argument("--score-tolerance", type=float, default=None)
    ap.add_argument("--json", dest="json_out", help="write the full report JSON to this path")
    args = ap.parse_args()

    from app import provider_compare

    if args.folder:
        limit = args.limit or None
        data_a = _run_one_provider(args.folder, "grok", limit=limit, mock=args.mock)
        data_b = _run_one_provider(args.folder, "qwen", limit=limit, mock=args.mock)
    elif args.run_a is not None and args.run_b is not None:
        data_a = _load_run(args.run_a)
        data_b = _load_run(args.run_b)
        if not data_a or not data_b:
            print("error: one or both runs not found", file=sys.stderr)
            return 1
    else:
        print("error: pass --run-a/--run-b or --folder", file=sys.stderr)
        return 1

    kwargs = {}
    if args.score_tolerance is not None:
        kwargs["score_tolerance"] = args.score_tolerance
    report = provider_compare.compare_provider_runs(data_a, data_b, **kwargs)

    _print_summary(report)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n  full report -> {args.json_out}")

    return 0 if report["verdict"]["within_tolerance"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
