"""Read-only Mise gallery index client (Phase 6 slice 1).

Proxies Argus operators to Mise's GET /api/galleries and supplies originals_path
for resolve_mise_folder when ARGUS_MISE_MEDIA_ROOT is unset but homelab paths are
still reachable (shared mount / same host).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx

from . import config, db, structured_log

log = logging.getLogger("argus.mise")

# HTTP statuses worth retrying (transient). 404/410 are treated as a no-op
# (the gallery is gone), other 4xx (incl. 401) as a hard failure -> dead-letter.
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


class MiseClientError(Exception):
    """Human-readable Mise API failure (no secrets)."""


def is_enabled() -> bool:
    return bool(config.MISE_URL and config.MISE_API_TOKEN)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.MISE_API_TOKEN}"}


def list_galleries(*, published: bool = True) -> dict[str, Any]:
    if not is_enabled():
        raise MiseClientError("Mise API is not configured")
    url = f"{config.MISE_URL}/api/galleries"
    params = {"published": "true" if published else "false"}
    try:
        with httpx.Client(timeout=config.MISE_TIMEOUT) as client:
            resp = client.get(url, params=params, headers=_headers())
    except httpx.TimeoutException as exc:
        raise MiseClientError(f"Mise API timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise MiseClientError(f"Mise API unreachable: {exc}") from exc

    if resp.status_code == 503:
        raise MiseClientError("Mise galleries API is disarmed")
    if resp.status_code == 401:
        raise MiseClientError("Mise API rejected the bearer token")
    if resp.status_code >= 400:
        raise MiseClientError(f"Mise API returned HTTP {resp.status_code}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise MiseClientError("Mise API returned unreadable JSON") from exc

    if not isinstance(body, dict) or "galleries" not in body:
        raise MiseClientError("Mise API returned an unexpected body")
    return body


def get_gallery(gallery_id: int) -> dict[str, Any] | None:
    body = list_galleries(published=False)
    for row in body.get("galleries") or []:
        if row.get("id") == gallery_id:
            return row
    return None


def _callback_headers(payload: dict[str, Any]) -> dict[str, str]:
    """Bearer + an Idempotency-Key mirror of the payload key, so Mise can dedupe
    re-deliveries at the HTTP layer without parsing the body."""
    headers = _headers()
    key = payload.get("idempotency_key")
    if key:
        headers["Idempotency-Key"] = str(key)
    return headers


def _attempt_post(gallery_id: int, payload: dict[str, Any]) -> tuple[str, str | None]:
    """One POST attempt. Returns (outcome, detail) where outcome is one of:
    delivered | noop (stale subject) | transient (retryable) | hard (do not retry)."""
    url = f"{config.MISE_URL}/api/argus/callback"
    try:
        with httpx.Client(timeout=config.MISE_TIMEOUT) as client:
            resp = client.post(
                url,
                params={"gallery_id": gallery_id},
                json=payload,
                headers=_callback_headers(payload),
                follow_redirects=False,
            )
    except httpx.RequestError as exc:
        return "transient", f"network: {exc}"
    code = resp.status_code
    if code < 300:
        return "delivered", str(code)
    if code in (404, 410):
        return "noop", str(code)
    if code == 401:
        return "auth", f"HTTP 401: {resp.text[:160]}"
    if code in _TRANSIENT_STATUS:
        return "transient", f"HTTP {code}: {resp.text[:160]}"
    return "hard", f"HTTP {code}: {resp.text[:160]}"


def _reload_mise_token() -> bool:
    """Re-read the Mise service token (token-drift recovery). Returns True only when
    a different, non-empty token was loaded — i.e. a retry is worth attempting."""
    old = config.MISE_API_TOKEN
    new = config.reload_mise_token()
    changed = bool(new) and new != old
    if changed:
        structured_log.event("callback.token_reloaded")
        log.warning("reloaded rotated Mise service token after 401")
    return changed


def _dead_letter(gallery_id: int, payload: dict[str, Any], last_status: str | None, detail: str | None) -> None:
    """Persist an undelivered callback so it is never lost, and surface it."""
    key = payload.get("idempotency_key") or f"argus-g{gallery_id}-r{payload.get('run_id')}"
    try:
        db.enqueue_dead_letter_callback(
            idempotency_key=str(key),
            gallery_id=gallery_id,
            run_id=payload.get("run_id"),
            payload=json.dumps(payload),
            last_status=last_status,
            last_error=detail,
        )
    except Exception as exc:  # persistence must not raise out of the delivery thread
        log.error("failed to dead-letter callback gallery %s: %s", gallery_id, exc)
    structured_log.event(
        "callback.dead_letter",
        gallery_id=gallery_id,
        run_id=payload.get("run_id"),
        idempotency_key=str(key),
        last_status=last_status,
    )
    log.error(
        "structured callback dead-lettered gallery %s run %s (%s) — re-deliverable via "
        "/admin/callbacks/redeliver",
        gallery_id,
        payload.get("run_id"),
        last_status,
    )
    _alert_dead_letter(gallery_id, str(key), last_status, detail)


def _alert_dead_letter(gallery_id: int, key: str, last_status: str | None, detail: str | None) -> None:
    """Best-effort operator alert so a dead-lettered run never fails silently."""
    url = config.CAP_WEBHOOK_URL
    if not url:
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                url,
                json={
                    "alert": "argus_callback_dead_letter",
                    "gallery_id": gallery_id,
                    "idempotency_key": key,
                    "last_status": last_status,
                    "last_error": detail,
                },
            )
    except Exception as exc:
        log.warning("dead-letter alert webhook failed: %s", exc)


def _deliver_once(gallery_id: int, payload: dict[str, Any]) -> tuple[str, str | None]:
    """One delivery, retrying transient failures with exponential backoff. Returns
    the terminal (outcome, detail): delivered | noop | auth | hard | transient
    (the last for exhausted transient retries)."""
    attempts = config.MISE_CALLBACK_MAX_ATTEMPTS
    last: tuple[str, str | None] = ("transient", "no attempt made")
    for attempt in range(1, attempts + 1):
        outcome, detail = _attempt_post(gallery_id, payload)
        if outcome in ("delivered", "noop", "auth", "hard"):
            return outcome, detail
        last = (outcome, detail)  # transient
        if attempt < attempts:
            backoff = config.MISE_CALLBACK_BACKOFF_BASE * (2 ** (attempt - 1))
            log.warning(
                "structured callback transient failure gallery %s (attempt %s/%s): %s — retrying in %.1fs",
                gallery_id, attempt, attempts, detail, backoff,
            )
            time.sleep(backoff)
    return last


def _deliver_with_retry(gallery_id: int, payload: dict[str, Any]) -> str:
    """Deliver durably. Transient failures retry with backoff; a 401 triggers a
    single token-drift re-auth (reload the Mise token + retry once); anything still
    unresolved is dead-lettered (never dropped). Returns the terminal outcome."""
    outcome, detail = _deliver_once(gallery_id, payload)
    if outcome == "delivered":
        log.info("structured callback delivered for gallery %s run %s", gallery_id, payload.get("run_id"))
        return "delivered"
    if outcome == "noop":
        log.info("structured callback no-op (stale subject %s) gallery %s", detail, gallery_id)
        return "noop"

    if outcome == "auth":
        # The known 401 outage class: an in-memory token gone stale after a
        # rotation. Reload from .env/env and retry ONCE; if the token did not
        # change (or the retry still 401s), dead-letter + alert — never silent.
        if _reload_mise_token():
            outcome, detail = _deliver_once(gallery_id, payload)
            if outcome == "delivered":
                log.info("structured callback delivered after re-auth for gallery %s", gallery_id)
                return "delivered"
            if outcome == "noop":
                return "noop"
        else:
            detail = f"{detail} (token unchanged after reload — check ARGUS_MISE_API_TOKEN)"
        structured_log.event("callback.auth_failure", gallery_id=gallery_id, run_id=payload.get("run_id"))
        log.error("structured callback auth failure gallery %s: %s", gallery_id, detail)

    _dead_letter(gallery_id, payload, outcome, detail)
    return "dead_letter"


def argus_callback(gallery_id: int, payload: dict[str, Any], *, background: bool = True) -> None:
    """Send a structured-output result to Mise's POST /api/argus/callback?gallery_id.

    No-ops unless Mise is configured (ARGUS_MISE_URL + ARGUS_MISE_API_TOKEN), so
    CI/dev never makes an HTTP call. Fires on a daemon thread by default so it
    never blocks the analyze response or the job worker loop. Transient failures
    are retried with exponential backoff; on exhaustion (or a hard failure) the
    payload is dead-lettered locally and re-deliverable — a completed run is never
    lost. The payload is deterministic and carries a stable idempotency_key, so a
    retry/re-delivery is safe (Mise dedupes)."""
    if not is_enabled():
        log.debug("structured callback skipped (Mise not configured) gallery %s", gallery_id)
        return
    if not background:
        _deliver_with_retry(gallery_id, payload)
        return
    threading.Thread(
        target=_deliver_with_retry,
        args=(gallery_id, payload),
        name=f"argus-mise-callback-{gallery_id}",
        daemon=True,
    ).start()


def redeliver_dead_letters(*, limit: int = 50) -> dict[str, int]:
    """Re-POST dead-lettered callbacks (single attempt each). On success the row
    is removed; on continued failure its attempt counter is bumped. Idempotent —
    the stable idempotency_key means Mise won't double-apply. No-op if Mise is
    unconfigured. Safe to call from the worker tick or POST /admin/callbacks/redeliver."""
    summary = {"attempted": 0, "delivered": 0, "still_failed": 0}
    if not is_enabled():
        return summary
    rows = db.list_dead_letter_callbacks(limit=limit)
    if rows:
        # Pick up a rotated token before re-POSTing, so a token-drift dead-letter
        # self-heals on the next worker tick / redeliver call.
        _reload_mise_token()
    for row in rows:
        summary["attempted"] += 1
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            db.resolve_dead_letter_callback(row["idempotency_key"])  # unparseable; drop
            continue
        outcome, detail = _attempt_post(row["gallery_id"], payload)
        if outcome in ("delivered", "noop"):
            db.resolve_dead_letter_callback(row["idempotency_key"])
            summary["delivered"] += 1
        else:
            db.bump_dead_letter_attempt(row["idempotency_key"], last_status=outcome, last_error=detail)
            summary["still_failed"] += 1
    if summary["attempted"]:
        log.info("dead-letter redelivery: %s", summary)
    return summary


def plutus_callback(
    gallery_id: int,
    *,
    run_id: int | None = None,
    status: str = "done",
    error: str | None = None,
    offer_url: str | None = None,
    review_url: str | None = None,
    pitch_url: str | None = None,
    bundle_count: int | None = None,
    estimated_total_cents: int | None = None,
) -> None:
    """Best-effort write-back so pipeline dashboard shows plutus_last_*."""
    if not is_enabled():
        return
    url = f"{config.MISE_URL}/api/plutus/callback"
    payload: dict[str, Any] = {"status": status}
    if run_id is not None:
        payload["run_id"] = run_id
    if error:
        payload["error"] = error
    if review_url:
        payload["review_url"] = review_url
    if pitch_url:
        payload["pitch_url"] = pitch_url
    if bundle_count is not None:
        payload["bundle_count"] = bundle_count
    if estimated_total_cents is not None:
        payload["estimated_total_cents"] = estimated_total_cents
    if offer_url:
        payload["offer_url"] = offer_url
    elif review_url:
        payload["offer_url"] = review_url
    try:
        with httpx.Client(timeout=config.MISE_TIMEOUT) as client:
            resp = client.post(
                url,
                params={"gallery_id": gallery_id},
                json=payload,
                headers=_headers(),
            )
    except httpx.RequestError as exc:
        log.warning("mise plutus callback unreachable gallery %s: %s", gallery_id, exc)
        return
    if resp.status_code >= 400:
        log.warning(
            "mise plutus callback HTTP %s gallery %s: %s",
            resp.status_code,
            gallery_id,
            resp.text[:200],
        )