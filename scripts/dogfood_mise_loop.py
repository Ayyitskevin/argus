#!/usr/bin/env python3
"""Homelab Mise ↔ Argus loop checklist (no xAI credits required for infra gates).

Usage:
    python scripts/dogfood_mise_loop.py
    python scripts/dogfood_mise_loop.py --trigger-admin --gallery-id 1

Exit 0 when all infra gates pass; 1 on config errors; 2 when a gate fails.
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


def _gate(gates: list[dict], gid: str, ok: bool, detail: str) -> None:
    gates.append({"id": gid, "pass": ok, "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {gid}: {detail}")


def _mise_url() -> str:
    return os.environ.get("ARGUS_MISE_URL", "http://flow:8400").rstrip("/")


def _argus_url() -> str:
    host = os.environ.get("ARGUS_PUBLIC_HOST", "127.0.0.1")
    port = os.environ.get("ARGUS_PORT", "8010")
    return f"http://{host}:{port}"


def _token() -> str:
    return os.environ.get("ARGUS_API_TOKEN") or os.environ.get("ARGUS_MISE_API_TOKEN", "")


def _media_root() -> Path:
    raw = os.environ.get("ARGUS_MISE_MEDIA_ROOT", str(ROOT / "data" / "mise-media"))
    return Path(raw).expanduser()


def check_infra(gates: list[dict]) -> bool:
    print("==> Infra gates")
    token = _token()
    if not token:
        _gate(gates, "token_configured", False, "ARGUS_API_TOKEN unset in .env")
        return False
    _gate(gates, "token_configured", True, "bearer token present")

    code, body = _get(f"{_argus_url()}/healthz")
    _gate(gates, "argus_health", code == 200 and '"status":"ok"' in body.replace(" ", ""),
          f"HTTP {code} from {_argus_url()}/healthz")

    code, body = _get(f"{_mise_url()}/healthz")
    _gate(gates, "mise_health", code == 200 and '"ok":true' in body.replace(" ", ""),
          f"HTTP {code} from {_mise_url()}/healthz")

    code, body = _get(f"{_mise_url()}/api/galleries?published=true", token=token)
    has_galleries = code == 200 and '"galleries"' in body
    _gate(gates, "mise_galleries_api", has_galleries, f"HTTP {code}")
    if not has_galleries:
        return False

    data = json.loads(body)
    galleries = data.get("galleries") or []
    _gate(gates, "published_gallery_exists", bool(galleries),
          f"{len(galleries)} published gallery(ies)")

    if galleries:
        g = galleries[0]
        media = _media_root() / str(g["id"]) / "original"
        n = len(list(media.glob("*"))) if media.is_dir() else 0
        _gate(gates, "local_media_synced", n > 0,
              f"{media} ({n} files) — run scripts/sync-mise-media.sh {g['id']}")

    code, _ = _get(f"{_argus_url()}/runs/1", token=token)
    _gate(gates, "review_ui_reachable", code in (200, 404),
          f"GET /runs/1 HTTP {code}")

    return all(g["pass"] for g in gates)


def trigger_analyze(gallery_id: int) -> dict:
    """POST /analyze-folder from flow (same path as mise job worker)."""
    token = _token()
    argus_host = os.environ.get("ARGUS_TAILNET_HOST", "strix-halo-a9-mega")
    argus_port = os.environ.get("ARGUS_PORT", "8010")
    base = os.environ.get("MISE_BASE_URL", "https://kleephotography.com").rstrip("/")
    callback = f"{base}/api/argus/callback?gallery_id={gallery_id}"
    body = urllib.parse.urlencode({
        "mise_gallery_id": gallery_id,
        "limit": int(os.environ.get("MISE_ARGUS_ANALYZE_LIMIT", "2")),
        "skip_dedup": "true",
        "source": "mise",
        "callback_url": callback,
    }).encode()
    url = f"http://{argus_host}:{argus_port}/analyze-folder"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    return payload


def wait_gallery_status(gallery_id: int, *, timeout: float = 120.0) -> dict:
    token = _token()
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        code, body = _get(f"{_mise_url()}/api/galleries?published=true", token=token)
        if code == 200:
            for row in json.loads(body).get("galleries") or []:
                if row.get("id") == gallery_id:
                    last = row
                    st = row.get("argus_last_status")
                    if st in ("done", "error"):
                        return row
        time.sleep(3)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description="Mise ↔ Argus homelab checklist")
    parser.add_argument("--trigger", action="store_true",
                        help="POST analyze-folder (mise job worker path) after infra gates")
    parser.add_argument("--gallery-id", type=int, default=1)
    parser.add_argument("--wait", action="store_true",
                        help="Poll gallery argus_last_* after trigger")
    args = parser.parse_args()

    _load_dotenv()
    gates: list[dict] = []
    ok = check_infra(gates)
    if not ok:
        print("\n==> Infra checklist FAILED")
        return 2

    if args.trigger:
        print(f"\n==> Trigger analyze gallery {args.gallery_id}")
        try:
            result = trigger_analyze(args.gallery_id)
            print(f"  mode={result.get('mode')} job={result.get('job_id')} run={result.get('run_id')}")
        except Exception as exc:
            print(f"  FAIL trigger: {exc}")
            return 2
        if args.wait:
            print("  waiting for argus_last_status …")
            row = wait_gallery_status(args.gallery_id)
            print(f"  status={row.get('argus_last_status')} run={row.get('argus_last_run_id')} "
                  f"job={row.get('argus_last_job_id')}")
            if row.get("argus_last_status") != "done":
                return 2

    print("\n==> Infra checklist PASSED (vision quality gate needs xAI credits + dogfood_proof.py)")
    report = {"gates": gates, "passed": True}
    out = ROOT / "data" / f"dogfood-mise-loop-{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())