#!/usr/bin/env python3
"""Homelab full pipeline: Mise → Argus (Grok) → Plutus upsell.

Runs infra gates, optionally triggers a real analyze on a published Mise gallery,
waits for Argus completion, then verifies Plutus received bundles.

Usage:
    python scripts/dogfood_full_loop.py
    python scripts/dogfood_full_loop.py --trigger --gallery-id 1 --limit 2
    python scripts/dogfood_full_loop.py --plutus-only --gallery-id 1 --argus-run-id 199

Exit 0 when all gates pass; 1 config error; 2 gate failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _get(url: str, *, token: str | None = None, timeout: float = 10.0) -> tuple[int, str]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def _post_form(
    url: str,
    fields: dict,
    *,
    token: str,
    timeout: float = 30.0,
) -> tuple[int, dict]:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def _gate(gates: list[dict], gid: str, ok: bool, detail: str) -> None:
    gates.append({"id": gid, "pass": ok, "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {gid}: {detail}")


def _token() -> str:
    return os.environ.get("ARGUS_API_TOKEN") or os.environ.get("ARGUS_MISE_API_TOKEN", "")


def _argus_url() -> str:
    host = os.environ.get("ARGUS_PUBLIC_HOST", "127.0.0.1")
    port = os.environ.get("ARGUS_PORT", "8010")
    return f"http://{host}:{port}"


def _mise_url() -> str:
    return os.environ.get("ARGUS_MISE_URL", "http://flow:8400").rstrip("/")


def _plutus_url() -> str:
    return os.environ.get("ARGUS_PLUTUS_URL", "http://127.0.0.1:8030").rstrip("/")


def _plutus_token() -> str:
    return os.environ.get("ARGUS_PLUTUS_TOKEN") or _token()


def check_infra(gates: list[dict]) -> bool:
    print("==> Infra gates")
    token = _token()
    if not token:
        _gate(gates, "token_configured", False, "ARGUS_API_TOKEN unset")
        return False
    _gate(gates, "token_configured", True, "bearer token present")

    code, body = _get(f"{_argus_url()}/healthz")
    ok = code == 200 and '"status":"ok"' in body.replace(" ", "")
    _gate(gates, "argus_health", ok, f"HTTP {code} from {_argus_url()}/healthz")
    if ok and '"backend":"grok"' in body.replace(" ", ""):
        _gate(gates, "argus_grok_backend", True, "vision backend is grok")
    elif ok:
        _gate(gates, "argus_grok_backend", False, "vision backend is not grok — set ARGUS_VISION_BACKEND=grok")

    code, body = _get(f"{_mise_url()}/healthz")
    _gate(gates, "mise_health", code == 200 and '"ok":true' in body.replace(" ", ""),
          f"HTTP {code} from {_mise_url()}/healthz")

    code, body = _get(f"{_plutus_url()}/healthz")
    _gate(gates, "plutus_health", code == 200 and '"status":"ok"' in body.replace(" ", ""),
          f"HTTP {code} from {_plutus_url()}/healthz")

    code, body = _get(f"{_mise_url()}/api/galleries?published=true", token=token)
    has_galleries = code == 200 and '"galleries"' in body
    _gate(gates, "mise_galleries_api", has_galleries, f"HTTP {code}")

    return all(g["pass"] for g in gates)


def trigger_analyze(gallery_id: int, *, limit: int) -> dict:
    token = _token()
    argus_host = os.environ.get("ARGUS_TAILNET_HOST", "127.0.0.1")
    argus_port = os.environ.get("ARGUS_PORT", "8010")
    base = os.environ.get("MISE_BASE_URL", "https://kleephotography.com").rstrip("/")
    callback = f"{base}/api/argus/callback?gallery_id={gallery_id}"
    fields = {
        "mise_gallery_id": gallery_id,
        "limit": limit,
        "skip_dedup": "true",
        "source": "mise",
        "callback_url": callback,
    }
    url = f"http://{argus_host}:{argus_port}/analyze-folder"
    code, payload = _post_form(url, fields, token=token, timeout=60)
    if code >= 400:
        raise RuntimeError(f"analyze-folder HTTP {code}: {payload}")
    return payload


def wait_argus_job(job_id: str, *, timeout: float = 300.0) -> dict:
    token = _token()
    deadline = time.time() + timeout
    url = f"{_argus_url()}/jobs/{job_id}"
    while time.time() < deadline:
        code, body = _get(url, token=token)
        if code == 200:
            job = json.loads(body)
            if job.get("status") in ("done", "failed", "dead_letter"):
                return job
        time.sleep(3)
    raise TimeoutError(f"job {job_id} did not complete within {timeout}s")


def wait_mise_argus(gallery_id: int, *, timeout: float = 300.0) -> dict:
    token = _token()
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        code, body = _get(f"{_mise_url()}/api/galleries?published=true", token=token)
        if code == 200:
            for row in json.loads(body).get("galleries") or []:
                if row.get("id") == gallery_id:
                    last = row
                    if row.get("argus_last_status") in ("done", "error"):
                        return row
        time.sleep(3)
    return last


def call_plutus(gallery_id: int, argus_run_id: int) -> dict:
    code, payload = _post_form(
        f"{_plutus_url()}/recommend/mise-gallery",
        {"mise_gallery_id": gallery_id, "argus_run_id": argus_run_id},
        token=_plutus_token(),
        timeout=60,
    )
    if code >= 400:
        raise RuntimeError(f"plutus recommend HTTP {code}: {payload}")
    return payload


def verify_plutus_run(run_id: int) -> dict:
    code, body = _get(f"{_plutus_url()}/runs/{run_id}/json", token=_plutus_token())
    if code != 200:
        raise RuntimeError(f"plutus run {run_id} HTTP {code}")
    return json.loads(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mise → Argus → Plutus homelab loop")
    parser.add_argument("--trigger", action="store_true", help="POST analyze-folder for gallery")
    parser.add_argument("--gallery-id", type=int, default=1)
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--wait", action="store_true", help="Wait for Argus job after trigger")
    parser.add_argument(
        "--plutus-only",
        action="store_true",
        help="Skip Argus trigger; POST Plutus recommend with --argus-run-id",
    )
    parser.add_argument("--argus-run-id", type=int, default=None)
    args = parser.parse_args()

    _load_dotenv()
    gates: list[dict] = []
    if not check_infra(gates):
        print("\n==> Infra FAILED")
        return 2

    argus_run_id = args.argus_run_id

    if args.plutus_only:
        if not argus_run_id:
            print("--plutus-only requires --argus-run-id", file=sys.stderr)
            return 1
        print(f"\n==> Plutus recommend gallery {args.gallery_id} + argus run {argus_run_id}")
    elif args.trigger:
        print(f"\n==> Trigger Argus analyze gallery {args.gallery_id} (limit={args.limit})")
        try:
            result = trigger_analyze(args.gallery_id, limit=args.limit)
            print(f"  mode={result.get('mode')} job={result.get('job_id')} run={result.get('run_id')}")
        except Exception as exc:
            print(f"  FAIL trigger: {exc}")
            return 2
        if args.wait:
            job_id = result.get("job_id")
            if job_id:
                print(f"  waiting for job {job_id} …")
                job = wait_argus_job(job_id)
                print(f"  job status={job.get('status')} run={job.get('run_id')}")
                if job.get("status") != "done":
                    return 2
                argus_run_id = int(job.get("run_id") or 0) or None
            else:
                argus_run_id = int(result.get("run_id") or 0) or None
        else:
            print("  (--wait not set — skipping Plutus verify)")
            print("\n==> Infra PASSED (trigger sent)")
            return 0
    else:
        row = wait_mise_argus(args.gallery_id, timeout=5)
        argus_run_id = row.get("argus_last_run_id")
        if argus_run_id:
            print(f"\n==> Using existing argus run {argus_run_id} for gallery {args.gallery_id}")
        else:
            print("\n==> No argus run — use --trigger to analyze first")
            print("==> Infra PASSED (no pipeline run)")
            return 0

    if not argus_run_id:
        print("  FAIL: no argus_run_id for Plutus handoff")
        return 2

    print(f"\n==> Plutus recommend (gallery {args.gallery_id}, argus run {argus_run_id})")
    try:
        plutus = call_plutus(args.gallery_id, int(argus_run_id))
    except Exception as exc:
        _gate(gates, "plutus_recommend", False, str(exc))
        print("\n==> Pipeline FAILED at Plutus")
        return 2

    run_id = int(plutus["run_id"])
    bundles = plutus.get("bundles") or []
    engine = plutus.get("engine", "mock")
    theme = plutus.get("gallery_theme", "—")
    _gate(
        gates,
        "plutus_recommend",
        bool(bundles),
        f"run={run_id} engine={engine} theme={theme} bundles={len(bundles)}",
    )

    try:
        row = verify_plutus_run(run_id)
        est = row.get("estimated_total_cents") or row.get("payload", {}).get("estimated_total_cents")
        _gate(gates, "plutus_run_persisted", True, f"est={est}")
    except Exception as exc:
        _gate(gates, "plutus_run_persisted", False, str(exc))

    review_url = plutus.get("review_url") or f"{_plutus_url()}/runs/{run_id}"
    pitch_url = plutus.get("pitch_url") or f"{_plutus_url()}/runs/{run_id}/pitch.txt"
    try:
        code, body = _get(review_url)
        _gate(
            gates,
            "plutus_review_url",
            code == 200 and ("Upsell bundles" in body or "bundle" in body.lower()),
            f"HTTP {code} {review_url}",
        )
        code, pitch_body = _get(pitch_url)
        _gate(
            gates,
            "plutus_pitch_url",
            code == 200 and len(pitch_body.strip()) > 20,
            f"HTTP {code} {pitch_url}",
        )
    except Exception as exc:
        _gate(gates, "plutus_review_url", False, str(exc))
        _gate(gates, "plutus_pitch_url", False, str(exc))

    ok = all(g["pass"] for g in gates)
    print(f"\n==> Full loop {'PASSED' if ok else 'FAILED'}")
    print(f"  Argus run: {_argus_url()}/runs/{argus_run_id}")
    print(f"  Plutus review: {review_url}")
    print(f"  Plutus pitch: {pitch_url}")

    report = {"gates": gates, "passed": ok, "argus_run_id": argus_run_id, "plutus_run_id": run_id}
    out = ROOT / "data" / f"dogfood-full-loop-{int(time.time())}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {out}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())