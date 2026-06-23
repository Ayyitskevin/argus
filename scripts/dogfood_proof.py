#!/usr/bin/env python3
"""Grok vision proof ladder — smoke + folder dogfood with pass/fail checklist.

No image generation. Uses existing photos on disk.

Usage:
    ARGUS_VISION_BACKEND=grok python scripts/dogfood_proof.py [folder] --limit 2

Writes a JSON report under ARGUS_DATA_DIR and prints a human checklist.
Exit 0 when all gates pass, 2 when vision/API fails, 1 on config errors.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ARGUS_VISION_BACKEND", "grok")

from app import config  # noqa: E402
from app.service import analyze_folder_run  # noqa: E402
from app.vision import analyze_image  # noqa: E402


def _default_folder() -> Path:
    for candidate in (
        ROOT / "data" / "demo",
        Path("/home/kevin-lee/ai-workspace/argus/data/demo"),
        Path("/home/kevin-lee/ai-workspace/argus/data/dogfood-gallery-grok"),
    ):
        if candidate.is_dir() and any(candidate.glob("*.jpg")):
            return candidate
    raise FileNotFoundError("no demo folder with JPEGs found")


def _photo_summary(photo: dict) -> dict:
    culling = photo.get("culling") or {}
    keywords = photo.get("keywords") or []
    return {
        "path": Path(photo.get("image_path", "")).name,
        "shot_type": photo.get("shot_type"),
        "keeper": culling.get("keeper_score"),
        "hero": culling.get("hero_potential"),
        "keyword_count": len(keywords),
        "degenerate": not keywords or keywords == ["analysis-failed"],
        "sample_keywords": keywords[:4],
        "notes": (culling.get("notes") or "")[:160],
    }


def _gate_checks(photos: list[dict]) -> list[dict]:
    gates = []
    if not photos:
        gates.append({"id": "has_photos", "pass": False, "detail": "no photos analyzed"})
        return gates

    degenerate = sum(1 for p in photos if _photo_summary(p)["degenerate"])
    rate = degenerate / len(photos)
    gates.append({
        "id": "degenerate_rate",
        "pass": rate < 0.1,
        "detail": f"{degenerate}/{len(photos)} degenerate ({rate:.0%})",
    })

    avg_keeper = sum(
        float((p.get("culling") or {}).get("keeper_score", 0) or 0) for p in photos
    ) / len(photos)
    gates.append({
        "id": "avg_keeper_sane",
        "pass": 0.05 <= avg_keeper <= 0.99,
        "detail": f"avg keeper {avg_keeper:.2f}",
    })

    specific = sum(
        1
        for p in photos
        if len(p.get("keywords") or []) >= 5
        and (p.get("keywords") or []) != ["analysis-failed"]
    )
    gates.append({
        "id": "keyword_richness",
        "pass": specific >= max(1, len(photos) // 2),
        "detail": f"{specific}/{len(photos)} with 5+ specific keywords",
    })
    return gates


def run_smoke(image: Path) -> dict:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "grok_smoke.py"),
        str(image),
    ]
    env = os.environ.copy()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(ROOT))
    return {
        "command": " ".join(cmd[-2:]),
        "exit_code": proc.returncode,
        "stdout": proc.stdout.strip()[-2000:],
        "stderr": proc.stderr.strip()[-2000:],
        "pass": proc.returncode == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus Grok proof ladder")
    parser.add_argument("folder", nargs="?", type=Path, help="folder to dogfood")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--client-id", default="proof")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    if args.data_dir:
        os.environ["ARGUS_DATA_DIR"] = args.data_dir

    report: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "backend": config.VISION_BACKEND,
        "model": config.VISION_MODEL,
        "api_key_configured": bool(config.XAI_API_KEY),
        "steps": [],
        "gates": [],
        "pass": False,
    }

    if config.VISION_BACKEND != "grok":
        print("ARGUS_VISION_BACKEND must be grok", file=sys.stderr)
        return 1
    if not config.XAI_API_KEY:
        print("XAI_API_KEY must be set", file=sys.stderr)
        return 1

    try:
        folder = (args.folder or _default_folder()).expanduser().resolve()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    images = sorted(folder.glob("*.jpg")) + sorted(folder.glob("*.jpeg"))
    if not images:
        print(f"No JPEGs in {folder}", file=sys.stderr)
        return 1

    print(f"Proof ladder: backend=grok model={config.VISION_MODEL}", flush=True)
    print(f"Folder: {folder} limit={args.limit}", flush=True)

    if not args.skip_smoke:
        print("Step 1/2: grok_smoke.py …", flush=True)
        smoke = run_smoke(images[0])
        report["steps"].append({"name": "smoke", **smoke})
        if not smoke["pass"]:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            _write_report(report)
            print("FAIL smoke — see stderr in report", file=sys.stderr)
            if "credit" in smoke["stderr"].lower():
                print("→ Add credits at https://console.x.ai", file=sys.stderr)
            return 2

    print("Step 2/2: folder dogfood …", flush=True)
    started = time.time()
    try:
        result = analyze_folder_run(
            folder=folder,
            source=f"client:{args.client_id}|proof:{folder}",
            limit=args.limit,
            client_id=args.client_id,
        )
    except Exception as exc:
        report["steps"].append({"name": "dogfood", "pass": False, "error": str(exc)})
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(report)
        print(f"FAIL dogfood: {exc}", file=sys.stderr)
        return 2

    elapsed = time.time() - started
    photos = result.get("photos") or []
    summaries = [_photo_summary(p) for p in photos]
    gates = _gate_checks(photos)
    report["steps"].append({
        "name": "dogfood",
        "pass": True,
        "run_id": result.get("run_id"),
        "count": result.get("count"),
        "elapsed_s": round(elapsed, 1),
        "photos": summaries,
    })
    report["gates"] = gates
    report["pass"] = all(g["pass"] for g in gates)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["run_url"] = f"/runs/{result.get('run_id')}"

    _write_report(report)
    _print_checklist(report)
    return 0 if report["pass"] else 2


def _write_report(report: dict) -> Path:
    out_dir = config.DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"dogfood-proof-{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {path}", flush=True)
    return path


def _print_checklist(report: dict) -> None:
    print("\n=== Proof checklist ===", flush=True)
    for step in report.get("steps", []):
        mark = "OK" if step.get("pass") else "FAIL"
        print(f"  [{mark}] {step.get('name')}", flush=True)
    for gate in report.get("gates", []):
        mark = "OK" if gate.get("pass") else "FAIL"
        print(f"  [{mark}] {gate.get('id')}: {gate.get('detail')}", flush=True)
    overall = "PASS" if report.get("pass") else "FAIL"
    print(f"\nOverall: {overall}", flush=True)
    if report.get("run_url"):
        print(f"Review: {report['run_url']}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())