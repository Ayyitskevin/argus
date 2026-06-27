"""SQLite storage for Argus.

The service is intentionally local-first: one SQLite file, short-lived
connections, WAL mode, and a tiny schema initialized on demand.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

from . import config

# Money is stored as integer micro-USD (1e-6 USD) so the accumulating usage
# ledger never drifts the way repeated float addition does. Public params and
# return keys stay in USD floats; conversion happens only at this db boundary.
MICRO_PER_USD = 1_000_000


def _usd_to_micros(usd: float | None) -> int | None:
    if usd is None:
        return None
    return int(round(float(usd) * MICRO_PER_USD))


def _micros_to_usd(micros: int | None) -> float | None:
    if micros is None:
        return None
    return micros / MICRO_PER_USD


class TenantScopeError(Exception):
    """Raised when a tenant-scoped query runs without a scope in SaaS mode."""


# Sentinel a caller passes to deliberately read across all tenants (admin /
# background worker). Distinct from None so an *accidental* omission of the
# tenant filter fails closed in SaaS mode instead of silently leaking rows.
GLOBAL_SCOPE = object()


def _resolve_tenant_scope(tenant_id):
    """Normalize a tenant scope to a filter value (str) or None (unscoped).

    Fail-closed: in SaaS mode an unspecified scope (None) is rejected; admin
    code must opt into the global view with GLOBAL_SCOPE. Homelab (non-SaaS)
    has no tenants, so None means "no filter" as before.
    """
    if tenant_id is GLOBAL_SCOPE:
        return None
    if tenant_id is None:
        if config.SAAS_MODE:
            raise TenantScopeError(
                "tenant scope required in SaaS mode (use db.GLOBAL_SCOPE for admin/global reads)"
            )
        return None
    return tenant_id


SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT,
    model TEXT,
    photo_count INTEGER DEFAULT 0,
    project_id TEXT
);

CREATE TABLE IF NOT EXISTS photo_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    shot_type TEXT,
    keywords TEXT,
    culling TEXT,
    alt_text TEXT,
    description TEXT,
    suggested_iptc TEXT,
    raw_response TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(run_id) REFERENCES analysis_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_photo_run ON photo_analyses(run_id);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',
    folder TEXT,
    limit_ INTEGER,
    write_sidecars BOOLEAN DEFAULT 0,
    sidecar_dir TEXT,
    run_id INTEGER,
    result TEXT,
    error TEXT,
    project_id TEXT,
    source TEXT,
    model TEXT,
    client_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT,
    style TEXT,
    prefs TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_prefs_client ON preferences(client_id, style);

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    vision_provider TEXT NOT NULL DEFAULT 'grok',
    cost_cap_micro_usd INTEGER,
    monthly_image_cap INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS tenant_api_keys (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at TEXT,
    FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_keys_prefix ON tenant_api_keys(key_prefix);

CREATE TABLE IF NOT EXISTS tenant_usage (
    tenant_id TEXT NOT NULL,
    period TEXT NOT NULL,
    images_analyzed INTEGER NOT NULL DEFAULT 0,
    cost_micro_usd INTEGER NOT NULL DEFAULT 0,
    grok_api_calls INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, period),
    FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    tenant_id TEXT,
    actor TEXT,
    action TEXT NOT NULL,
    resource TEXT,
    status TEXT,
    detail TEXT,
    ip TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, created_at);

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cap_alert_log (
    tenant_id TEXT NOT NULL,
    period TEXT NOT NULL,
    alert_kind TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, period, alert_kind),
    FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS callback_outbox (
    idempotency_key TEXT PRIMARY KEY,
    gallery_id INTEGER,
    run_id INTEGER,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_status TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mise_analyze_ledger (
    dedup_key TEXT PRIMARY KEY,
    mise_gallery_id INTEGER NOT NULL,
    client_id TEXT,
    run_id INTEGER,
    job_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_mise_ledger_gallery ON mise_analyze_ledger(mise_gallery_id);
"""

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def _apply_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    for table, column, typ in [
        ("photo_analyses", "shot_type", "TEXT"),
        ("photo_analyses", "width", "INTEGER"),
        ("photo_analyses", "height", "INTEGER"),
        ("analysis_runs", "project_id", "TEXT"),
        ("analysis_runs", "archived_at", "TEXT"),
        ("jobs", "project_id", "TEXT"),
        ("jobs", "source", "TEXT"),
        ("jobs", "model", "TEXT"),
        ("jobs", "client_id", "TEXT"),
        ("jobs", "callback_url", "TEXT"),
        ("jobs", "recursive", "BOOLEAN"),
        ("jobs", "retry_count", "INTEGER"),
        ("analysis_runs", "tenant_id", "TEXT"),
        ("jobs", "tenant_id", "TEXT"),
        ("preferences", "tenant_id", "TEXT"),
        ("tenants", "stripe_customer_id", "TEXT"),
        ("tenants", "stripe_subscription_id", "TEXT"),
        ("tenants", "billing_status", "TEXT"),
        ("tenants", "plan_tier", "TEXT"),
        ("tenants", "cost_cap_micro_usd", "INTEGER"),
        ("tenant_usage", "cost_micro_usd", "INTEGER NOT NULL DEFAULT 0"),
        ("mise_analyze_ledger", "folder_fingerprint", "TEXT"),
        # Structured-output cost report (Mise vision cutover): per-image accounting
        # summed per run for the /api/argus/callback ai_runs ledger.
        ("photo_analyses", "cost_micro_usd", "INTEGER"),
        ("photo_analyses", "latency_ms", "REAL"),
        # Mise shadow-pair correlation id echoed back on the structured callback.
        ("jobs", "correlation_id", "TEXT"),
    ]:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typ}")
        except sqlite3.OperationalError as exc:
            # Only a re-add of an existing column is expected here; anything else
            # (e.g. a typo'd table) must fail loud rather than be swallowed.
            if "duplicate column name" not in str(exc).lower():
                raise
    con.commit()


def init() -> None:
    """Create or upgrade the schema once per process."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        con = sqlite3.connect(config.DB_PATH, timeout=30)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            _apply_schema(con)
            _SCHEMA_READY = True
        finally:
            con.close()


def connect() -> sqlite3.Connection:
    init()
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def close(con: sqlite3.Connection) -> None:
    con.close()


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        close(con)


def _json_or(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _job_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    if data.get("result"):
        data["result"] = _json_or(data["result"], {})
    return data


def create_run(
    source: str | None,
    model: str,
    project_id: str | None = None,
    tenant_id: str | None = None,
) -> int:
    with tx() as con:
        cur = con.execute(
            "INSERT INTO analysis_runs (source, model, project_id, tenant_id) VALUES (?, ?, ?, ?)",
            (source, model, project_id, tenant_id),
        )
        return int(cur.lastrowid)


def set_run_photo_count(run_id: int, count: int) -> None:
    with tx() as con:
        con.execute(
            "UPDATE analysis_runs SET photo_count = ? WHERE id = ?",
            (count, run_id),
        )


def save_photo_analysis(run_id: int, data: dict) -> int:
    with tx() as con:
        cur = con.execute(
            """INSERT INTO photo_analyses
               (run_id, image_path, width, height, shot_type, keywords, culling,
                alt_text, description, suggested_iptc, raw_response,
                cost_micro_usd, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                data["image_path"],
                data.get("width"),
                data.get("height"),
                data.get("shot_type", "other"),
                json.dumps(data.get("keywords") or []),
                json.dumps(data.get("culling") or {}),
                data.get("alt_text"),
                data.get("description"),
                json.dumps(data.get("suggested_iptc") or {}),
                data.get("raw_response"),
                _usd_to_micros(data.get("cost_usd")),
                data.get("latency_ms"),
            ),
        )
        return int(cur.lastrowid)


def get_run(run_id: int, *, tenant_id: str | None = None) -> sqlite3.Row | None:
    tenant_id = _resolve_tenant_scope(tenant_id)
    con = connect()
    try:
        if tenant_id:
            return con.execute(
                "SELECT * FROM analysis_runs WHERE id=? AND tenant_id=?",
                (run_id, tenant_id),
            ).fetchone()
        return con.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
    finally:
        close(con)


def get_photo_image_path(photo_id: int, *, tenant_id: str | None = None) -> str | None:
    tenant_id = _resolve_tenant_scope(tenant_id)
    con = connect()
    try:
        if tenant_id:
            row = con.execute(
                """SELECT p.image_path FROM photo_analyses p
                   JOIN analysis_runs r ON r.id = p.run_id
                   WHERE p.id=? AND r.tenant_id=?""",
                (photo_id, tenant_id),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT image_path FROM photo_analyses WHERE id=?",
                (photo_id,),
            ).fetchone()
        return row["image_path"] if row else None
    finally:
        close(con)


def get_photos_for_run(run_id: int) -> list[sqlite3.Row]:
    con = connect()
    try:
        return con.execute(
            "SELECT * FROM photo_analyses WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    finally:
        close(con)


def get_photo_for_run(run_id: int, photo_id: int) -> dict | None:
    """Return one photo dict for a run, or None if missing or mismatched."""
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM photo_analyses WHERE id=? AND run_id=?",
            (photo_id, run_id),
        ).fetchone()
        if row is None:
            return None
        photo = dict(row)
        photo["keywords"] = _json_or(photo.get("keywords"), [])
        photo["culling"] = _json_or(photo.get("culling"), {})
        photo["suggested_iptc"] = _json_or(photo.get("suggested_iptc"), {})
        shot_type = photo.get("shot_type") or "other"
        photo["shot_type"] = str(shot_type).strip().lower().replace(" ", "_") or "other"
        photo["basename"] = os.path.basename(str(photo.get("image_path") or ""))
        return photo
    finally:
        close(con)


def update_photo_analysis(
    run_id: int,
    photo_id: int,
    *,
    keywords: list[str] | None = None,
    culling: dict | None = None,
    shot_type: str | None = None,
) -> dict | None:
    """Patch keywords, culling fields, and/or shot_type for one photo."""
    photo = get_photo_for_run(run_id, photo_id)
    if photo is None:
        return None

    updates: dict[str, object] = {}
    if keywords is not None:
        cleaned = [str(tag).strip() for tag in keywords if str(tag).strip()]
        updates["keywords"] = json.dumps(cleaned)
        photo["keywords"] = cleaned
    if culling is not None:
        merged = dict(photo.get("culling") or {})
        merged.update(culling)
        updates["culling"] = json.dumps(merged)
        photo["culling"] = merged
    if shot_type is not None:
        normalized = str(shot_type).strip().lower().replace(" ", "_") or "other"
        updates["shot_type"] = normalized
        photo["shot_type"] = normalized

    if not updates:
        return photo

    assignments = ", ".join(f"{key}=?" for key in updates)
    values = list(updates.values()) + [photo_id, run_id]
    with tx() as con:
        con.execute(
            f"UPDATE photo_analyses SET {assignments} WHERE id=? AND run_id=?",
            values,
        )
    return photo


def list_recent_runs(
    limit: int = 20,
    *,
    include_archived: bool = False,
    tenant_id: str | None = None,
) -> list[sqlite3.Row]:
    tenant_id = _resolve_tenant_scope(tenant_id)
    con = connect()
    try:
        clauses: list[str] = []
        params: list[object] = []
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if tenant_id:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return con.execute(
            f"SELECT * FROM analysis_runs {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        close(con)


def archive_run(run_id: int) -> bool:
    with tx() as con:
        cur = con.execute(
            "UPDATE analysis_runs SET archived_at=datetime('now') WHERE id=? AND archived_at IS NULL",
            (run_id,),
        )
        return cur.rowcount > 0


def get_full_run(run_id: int, *, tenant_id: str | None = None) -> dict | None:
    run = get_run(run_id, tenant_id=tenant_id)
    if not run:
        return None

    photos = []
    for row in get_photos_for_run(run_id):
        photo = dict(row)
        photo["keywords"] = _json_or(photo.get("keywords"), [])
        photo["culling"] = _json_or(photo.get("culling"), {})
        photo["suggested_iptc"] = _json_or(photo.get("suggested_iptc"), {})
        shot_type = photo.get("shot_type") or "other"
        photo["shot_type"] = str(shot_type).strip().lower().replace(" ", "_") or "other"
        photo["basename"] = os.path.basename(str(photo.get("image_path") or ""))
        photos.append(photo)

    return {"run": dict(run), "photos": photos}


def create_job(
    folder: str,
    limit: int = 20,
    write_sidecars: bool = False,
    sidecar_dir: str | None = None,
    project_id: str | None = None,
    source: str | None = None,
    model: str | None = None,
    client_id: str | None = None,
    callback_url: str | None = None,
    recursive: bool = False,
    tenant_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    job_id = str(uuid.uuid4())
    with tx() as con:
        con.execute(
            """INSERT INTO jobs
               (id, status, folder, limit_, write_sidecars, sidecar_dir,
                project_id, source, model, client_id, callback_url, recursive, retry_count,
                tenant_id, correlation_id)
               VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                job_id,
                folder,
                limit,
                int(write_sidecars),
                sidecar_dir,
                project_id,
                source,
                model,
                client_id,
                callback_url,
                int(recursive),
                tenant_id,
                correlation_id,
            ),
        )
    return job_id


def get_job(job_id: str, *, tenant_id: str | None = None) -> dict | None:
    tenant_id = _resolve_tenant_scope(tenant_id)
    con = connect()
    try:
        if tenant_id:
            row = con.execute(
                "SELECT * FROM jobs WHERE id=? AND tenant_id=?",
                (job_id, tenant_id),
            ).fetchone()
        else:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_dict(row)
    finally:
        close(con)


def update_job_progress(
    job_id: str,
    *,
    done: int,
    total: int,
    run_id: int | None = None,
    current_file: str | None = None,
) -> None:
    """Persist in-flight folder analyze progress on the job row."""
    progress: dict[str, object] = {"done": int(done), "total": int(total)}
    if current_file:
        progress["current"] = current_file
    kwargs: dict[str, object] = {"result": {"progress": progress}}
    if run_id is not None:
        kwargs["run_id"] = run_id
    update_job(job_id, **kwargs)


def update_job(job_id: str, **kwargs) -> None:
    if not kwargs:
        return
    if "result" in kwargs and kwargs["result"] is not None:
        kwargs["result"] = json.dumps(kwargs["result"])
    assignments = ", ".join(f"{key}=?" for key in kwargs)
    values = list(kwargs.values()) + [job_id]
    with tx() as con:
        con.execute(
            f"UPDATE jobs SET {assignments}, updated_at=datetime('now') WHERE id=?",
            values,
        )


def claim_next_job() -> dict | None:
    """Atomically claim the oldest queued job."""
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        if row is None:
            con.commit()
            return None
        job_id = row["id"]
        con.execute(
            "UPDATE jobs SET status='running', updated_at=datetime('now')"
            " WHERE id=? AND status='queued'",
            (job_id,),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        close(con)
    return get_job(job_id, tenant_id=GLOBAL_SCOPE)


def list_jobs(
    limit: int = 20,
    status: str | None = None,
    *,
    tenant_id: str | None = None,
) -> list[sqlite3.Row]:
    tenant_id = _resolve_tenant_scope(tenant_id)
    con = connect()
    try:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if tenant_id:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return con.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        close(con)


def count_jobs_by_status(status: str) -> int:
    con = connect()
    try:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status=?",
            (status,),
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        close(con)


def queue_depth() -> int:
    return count_jobs_by_status("queued")


def cleanup_old_jobs(days: int | None = None) -> None:
    days = days if days is not None else 90
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with tx() as con:
        con.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'failed', 'dead_letter') AND updated_at < ?",
            (cutoff,),
        )


def reconcile_stale_running_jobs(
    *,
    max_age_minutes: int | None = None,
    new_status: str = "failed",
    error: str = "stale: worker lost (process restart or crash)",
) -> int:
    """Mark orphaned ``running`` jobs terminal so the queue can drain cleanly."""
    if new_status not in {"failed", "dead_letter", "queued"}:
        raise ValueError(f"unsupported new_status: {new_status}")

    sql = "SELECT id FROM jobs WHERE status='running'"
    params: list = []
    if max_age_minutes is not None:
        sql += " AND updated_at < datetime('now', ?)"
        params.append(f"-{int(max_age_minutes)} minutes")

    con = connect()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        close(con)

    count = 0
    for row in rows:
        update_job(
            row["id"],
            status=new_status,
            error=error if new_status != "queued" else None,
        )
        count += 1
    return count


def purge_jobs(
    *,
    statuses: tuple[str, ...] | None = None,
    folder_prefixes: tuple[str, ...] | None = None,
) -> int:
    """Delete jobs matching optional status and/or ephemeral folder prefixes."""
    clauses: list[str] = []
    params: list = []

    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)

    if folder_prefixes:
        prefix_clauses = []
        for prefix in folder_prefixes:
            prefix_clauses.append("folder LIKE ?")
            params.append(f"{prefix}%")
        clauses.append("(" + " OR ".join(prefix_clauses) + ")")

    if not clauses:
        return 0

    where = " AND ".join(clauses)
    with tx() as con:
        cur = con.execute(f"DELETE FROM jobs WHERE {where}", params)
        return int(cur.rowcount)


def get_preferences(
    client_id: Optional[str] = None,
    style: Optional[str] = None,
    *,
    tenant_id: str | None = None,
) -> dict:
    tenant_id = _resolve_tenant_scope(tenant_id)
    con = connect()
    try:
        if client_id:
            if tenant_id:
                row = con.execute(
                    "SELECT prefs FROM preferences WHERE client_id=? AND tenant_id=?"
                    " ORDER BY updated_at DESC LIMIT 1",
                    (client_id, tenant_id),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT prefs FROM preferences WHERE client_id=?"
                    " ORDER BY updated_at DESC LIMIT 1",
                    (client_id,),
                ).fetchone()
            if row:
                return _json_or(row["prefs"], {})
        if style:
            if tenant_id:
                row = con.execute(
                    "SELECT prefs FROM preferences WHERE style=? AND tenant_id=?"
                    " ORDER BY updated_at DESC LIMIT 1",
                    (style, tenant_id),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT prefs FROM preferences WHERE style=?"
                    " ORDER BY updated_at DESC LIMIT 1",
                    (style,),
                ).fetchone()
            if row:
                return _json_or(row["prefs"], {})
        return {}
    finally:
        close(con)


def set_preferences(
    client_id: str,
    prefs: dict,
    style: Optional[str] = None,
    *,
    tenant_id: str | None = None,
) -> int:
    tenant_id = _resolve_tenant_scope(tenant_id)
    prefs_json = json.dumps(prefs or {})
    with tx() as con:
        if tenant_id:
            con.execute(
                "DELETE FROM preferences WHERE client_id=? AND (style IS ? OR style=?)"
                " AND tenant_id=?",
                (client_id, style, style, tenant_id),
            )
        else:
            con.execute(
                "DELETE FROM preferences WHERE client_id=? AND (style IS ? OR style=?)",
                (client_id, style, style),
            )
        cur = con.execute(
            "INSERT INTO preferences (client_id, style, prefs, tenant_id, updated_at)"
            " VALUES (?, ?, ?, ?, datetime('now'))",
            (client_id, style, prefs_json, tenant_id),
        )
        return int(cur.lastrowid)


def _client_source_pattern(client_id: str) -> str:
    return f"%client:{client_id}%"


def get_client_history_stats(client_id: str, *, tenant_id: str | None = None) -> dict:
    """Aggregate prior runs and photo analyses for history-based prefs (Phase 4)."""
    tenant_id = _resolve_tenant_scope(tenant_id)
    pattern = _client_source_pattern(client_id)
    con = connect()
    try:
        if tenant_id:
            run_rows = con.execute(
                "SELECT id FROM analysis_runs WHERE source LIKE ? AND tenant_id=?"
                " ORDER BY id DESC LIMIT 50",
                (pattern, tenant_id),
            ).fetchall()
        else:
            run_rows = con.execute(
                "SELECT id FROM analysis_runs WHERE source LIKE ? ORDER BY id DESC LIMIT 50",
                (pattern,),
            ).fetchall()
        num_runs = len(run_rows)
        if not run_rows:
            return {
                "client_id": client_id,
                "num_runs": 0,
                "num_photos": 0,
                "bias": 0.0,
                "top_shot_type": None,
                "shot_type_counts": {},
                "top_keywords": [],
                "avg_keeper_score": None,
                "avg_hero_potential": None,
            }

        run_ids = [row["id"] for row in run_rows]
        placeholders = ",".join("?" * len(run_ids))
        photo_rows = con.execute(
            f"""SELECT shot_type, keywords, culling
                FROM photo_analyses
                WHERE run_id IN ({placeholders})""",
            run_ids,
        ).fetchall()

        shot_type_counts: dict[str, int] = {}
        keyword_counts: dict[str, int] = {}
        keeper_scores: list[float] = []
        hero_scores: list[float] = []

        for row in photo_rows:
            shot_type = (row["shot_type"] or "other").strip().lower().replace(" ", "_")
            shot_type_counts[shot_type] = shot_type_counts.get(shot_type, 0) + 1
            for keyword in _json_or(row["keywords"], []):
                key = str(keyword).strip()
                if key:
                    keyword_counts[key] = keyword_counts.get(key, 0) + 1
            culling = _json_or(row["culling"], {})
            try:
                keeper_scores.append(float(culling.get("keeper_score", 0.0)))
            except (TypeError, ValueError):
                pass
            try:
                hero_scores.append(float(culling.get("hero_potential", 0.0)))
            except (TypeError, ValueError):
                pass

        top_shot_type = (
            max(shot_type_counts, key=shot_type_counts.get) if shot_type_counts else None
        )
        top_keywords = [
            key
            for key, _ in sorted(keyword_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
        ]

        return {
            "client_id": client_id,
            "num_runs": num_runs,
            "num_photos": len(photo_rows),
            "bias": min(0.05 * num_runs, 0.2),
            "top_shot_type": top_shot_type,
            "shot_type_counts": shot_type_counts,
            "top_keywords": top_keywords,
            "avg_keeper_score": round(sum(keeper_scores) / len(keeper_scores), 3)
            if keeper_scores
            else None,
            "avg_hero_potential": round(sum(hero_scores) / len(hero_scores), 3)
            if hero_scores
            else None,
        }
    finally:
        close(con)


def _tenant_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    keys = row.keys()
    return {
        "id": row["id"],
        "name": row["name"],
        "active": bool(row["active"]),
        "vision_provider": row["vision_provider"],
        "cost_cap_usd": _micros_to_usd(row["cost_cap_micro_usd"]),
        "monthly_image_cap": row["monthly_image_cap"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "stripe_customer_id": row["stripe_customer_id"] if "stripe_customer_id" in keys else None,
        "stripe_subscription_id": row["stripe_subscription_id"]
        if "stripe_subscription_id" in keys
        else None,
        "billing_status": row["billing_status"] if "billing_status" in keys else None,
        "plan_tier": row["plan_tier"] if "plan_tier" in keys else None,
    }


def create_tenant(
    tenant_id: str,
    *,
    name: str,
    vision_provider: str = "grok",
    cost_cap_usd: float | None = None,
    monthly_image_cap: int | None = None,
) -> dict:
    with tx() as con:
        con.execute(
            """INSERT INTO tenants (id, name, vision_provider, cost_cap_micro_usd, monthly_image_cap, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (tenant_id, name, vision_provider, _usd_to_micros(cost_cap_usd), monthly_image_cap),
        )
    tenant = get_tenant(tenant_id)
    assert tenant is not None
    return tenant


def get_tenant(tenant_id: str) -> dict | None:
    con = connect()
    try:
        row = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        return _tenant_dict(row)
    finally:
        close(con)


def list_tenants(*, active_only: bool = False) -> list[dict]:
    con = connect()
    try:
        sql = "SELECT * FROM tenants"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY id"
        return [_tenant_dict(row) for row in con.execute(sql).fetchall()]
    finally:
        close(con)


def update_tenant(tenant_id: str, **fields) -> dict | None:
    allowed = {
        "name",
        "active",
        "vision_provider",
        "cost_cap_usd",
        "monthly_image_cap",
        "stripe_customer_id",
        "stripe_subscription_id",
        "billing_status",
        "plan_tier",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_tenant(tenant_id)
    if "active" in updates:
        updates["active"] = 1 if updates["active"] else 0
    if "cost_cap_usd" in updates:
        updates["cost_cap_micro_usd"] = _usd_to_micros(updates.pop("cost_cap_usd"))
    assignments = ", ".join(f"{key}=?" for key in updates)
    values = list(updates.values()) + [tenant_id]
    with tx() as con:
        con.execute(
            f"UPDATE tenants SET {assignments}, updated_at=datetime('now') WHERE id=?",
            values,
        )
    return get_tenant(tenant_id)


def insert_tenant_api_key(
    *,
    key_id: str,
    tenant_id: str,
    key_prefix: str,
    key_hash: str,
    label: str | None = None,
) -> None:
    with tx() as con:
        con.execute(
            """INSERT INTO tenant_api_keys (id, tenant_id, key_prefix, key_hash, label)
               VALUES (?, ?, ?, ?, ?)""",
            (key_id, tenant_id, key_prefix, key_hash, label),
        )


def find_tenant_by_key_prefix(key_prefix: str) -> list[dict]:
    con = connect()
    try:
        rows = con.execute(
            """SELECT k.id AS key_id, k.key_hash, k.revoked_at, k.label,
                      t.id AS tenant_id, t.name, t.active, t.vision_provider,
                      t.cost_cap_micro_usd, t.monthly_image_cap, t.created_at, t.updated_at
               FROM tenant_api_keys k
               JOIN tenants t ON t.id = k.tenant_id
               WHERE k.key_prefix=? AND k.revoked_at IS NULL AND t.active=1""",
            (key_prefix,),
        ).fetchall()
        out = []
        for row in rows:
            tenant = {
                "id": row["tenant_id"],
                "name": row["name"],
                "active": bool(row["active"]),
                "vision_provider": row["vision_provider"],
                "cost_cap_usd": _micros_to_usd(row["cost_cap_micro_usd"]),
                "monthly_image_cap": row["monthly_image_cap"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            out.append({"key_id": row["key_id"], "key_hash": row["key_hash"], "tenant": tenant})
        return out
    finally:
        close(con)


def revoke_tenant_api_key(key_id: str) -> bool:
    with tx() as con:
        cur = con.execute(
            "UPDATE tenant_api_keys SET revoked_at=datetime('now') WHERE id=? AND revoked_at IS NULL",
            (key_id,),
        )
        return cur.rowcount > 0


def list_tenant_keys(tenant_id: str) -> list[dict]:
    con = connect()
    try:
        rows = con.execute(
            """SELECT id, tenant_id, key_prefix, label, created_at, revoked_at
               FROM tenant_api_keys WHERE tenant_id=? ORDER BY created_at DESC""",
            (tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        close(con)


def _usage_period(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    return dt.strftime("%Y-%m")


def get_tenant_usage(tenant_id: str, period: str | None = None) -> dict:
    period = period or _usage_period()
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM tenant_usage WHERE tenant_id=? AND period=?",
            (tenant_id, period),
        ).fetchone()
        if row is None:
            return {
                "tenant_id": tenant_id,
                "period": period,
                "images_analyzed": 0,
                "cost_usd": 0.0,
                "grok_api_calls": 0,
            }
        return {
            "tenant_id": tenant_id,
            "period": period,
            "images_analyzed": int(row["images_analyzed"]),
            "cost_usd": _micros_to_usd(int(row["cost_micro_usd"])),
            "grok_api_calls": int(row["grok_api_calls"]),
            "updated_at": row["updated_at"],
        }
    finally:
        close(con)


def increment_tenant_usage(
    tenant_id: str,
    *,
    images: int = 0,
    cost_usd: float = 0.0,
    grok_api_calls: int = 0,
    period: str | None = None,
) -> dict:
    period = period or _usage_period()
    with tx() as con:
        con.execute(
            """INSERT INTO tenant_usage (tenant_id, period, images_analyzed, cost_micro_usd, grok_api_calls, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(tenant_id, period) DO UPDATE SET
                 images_analyzed = images_analyzed + excluded.images_analyzed,
                 cost_micro_usd = cost_micro_usd + excluded.cost_micro_usd,
                 grok_api_calls = grok_api_calls + excluded.grok_api_calls,
                 updated_at = datetime('now')""",
            (tenant_id, period, images, _usd_to_micros(cost_usd) or 0, grok_api_calls),
        )
    return get_tenant_usage(tenant_id, period)


def charge_tenant_usage(
    tenant_id: str,
    *,
    images: int,
    cost_usd: float,
    grok_api_calls: int = 0,
    period: str | None = None,
    global_cost_cap_usd: float = 0.0,
    global_monthly_image_cap: int = 0,
) -> dict:
    """Atomically verify caps and increment tenant usage (BEGIN IMMEDIATE)."""
    if images <= 0:
        return get_tenant_usage(tenant_id, period)

    period = period or _usage_period()
    charge_micros = _usd_to_micros(cost_usd) or 0
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        tenant_row = con.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if tenant_row is None:
            con.commit()
            return get_tenant_usage(tenant_id, period)

        usage_row = con.execute(
            "SELECT * FROM tenant_usage WHERE tenant_id=? AND period=?",
            (tenant_id, period),
        ).fetchone()
        current_images = int(usage_row["images_analyzed"]) if usage_row else 0
        current_micros = int(usage_row["cost_micro_usd"]) if usage_row else 0

        if global_monthly_image_cap > 0:
            global_row = con.execute(
                """SELECT COALESCE(SUM(images_analyzed), 0) AS images
                   FROM tenant_usage WHERE period=?""",
                (period,),
            ).fetchone()
            global_images = int(global_row["images"]) if global_row else 0
            if global_images + images > global_monthly_image_cap:
                con.rollback()
                raise _UsageCapExceeded("global monthly image cap reached")

        global_cap_micros = _usd_to_micros(global_cost_cap_usd) or 0
        if global_cap_micros > 0:
            global_row = con.execute(
                """SELECT COALESCE(SUM(cost_micro_usd), 0) AS cost
                   FROM tenant_usage WHERE period=?""",
                (period,),
            ).fetchone()
            global_micros = int(global_row["cost"]) if global_row else 0
            if global_micros + charge_micros > global_cap_micros:
                con.rollback()
                raise _UsageCapExceeded("global cloud cost cap reached")

        tenant = _tenant_dict(tenant_row)
        cap_micros = _usd_to_micros(tenant.get("cost_cap_usd"))
        if cap_micros is not None and cap_micros > 0 and current_micros + charge_micros > cap_micros:
            con.rollback()
            raise _UsageCapExceeded(f"tenant {tenant_id} cost cap reached")

        image_cap = tenant.get("monthly_image_cap")
        if image_cap is not None and image_cap > 0 and current_images + images > int(image_cap):
            con.rollback()
            raise _UsageCapExceeded(f"tenant {tenant_id} monthly image cap reached")

        con.execute(
            """INSERT INTO tenant_usage (tenant_id, period, images_analyzed, cost_micro_usd, grok_api_calls, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(tenant_id, period) DO UPDATE SET
                 images_analyzed = images_analyzed + excluded.images_analyzed,
                 cost_micro_usd = cost_micro_usd + excluded.cost_micro_usd,
                 grok_api_calls = grok_api_calls + excluded.grok_api_calls,
                 updated_at = datetime('now')""",
            (tenant_id, period, images, charge_micros, grok_api_calls),
        )
        con.commit()
    except _UsageCapExceeded:
        raise
    except Exception:
        con.rollback()
        raise
    finally:
        close(con)
    return get_tenant_usage(tenant_id, period)


class _UsageCapExceeded(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def global_usage_totals(period: str | None = None) -> dict:
    period = period or _usage_period()
    con = connect()
    try:
        row = con.execute(
            """SELECT COALESCE(SUM(images_analyzed), 0) AS images,
                      COALESCE(SUM(cost_micro_usd), 0) AS cost,
                      COALESCE(SUM(grok_api_calls), 0) AS grok_calls
               FROM tenant_usage WHERE period=?""",
            (period,),
        ).fetchone()
        return {
            "period": period,
            "images_analyzed": int(row["images"]),
            "cost_usd": _micros_to_usd(int(row["cost"])),
            "grok_api_calls": int(row["grok_calls"]),
        }
    finally:
        close(con)


def insert_audit_event(
    *,
    action: str,
    tenant_id: str | None = None,
    actor: str | None = None,
    resource: str | None = None,
    status: str | None = None,
    detail: dict | str | None = None,
    ip: str | None = None,
) -> int:
    payload = json.dumps(detail) if isinstance(detail, dict) else detail
    with tx() as con:
        cur = con.execute(
            """INSERT INTO audit_log (tenant_id, actor, action, resource, status, detail, ip)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tenant_id, actor, action, resource, status, payload, ip),
        )
        return int(cur.lastrowid)


def list_audit_events(
    *,
    tenant_id: str | None = None,
    limit: int = 50,
    action: str | None = None,
) -> list[dict]:
    con = connect()
    try:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        if action:
            clauses.append("action=?")
            params.append(action)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = con.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = _json_or(item.get("detail"), item.get("detail"))
            out.append(item)
        return out
    finally:
        close(con)


def cleanup_audit_log(days: int | None = None) -> int:
    days = days if days is not None else 90
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with tx() as con:
        cur = con.execute("DELETE FROM audit_log WHERE created_at < ?", (cutoff,))
        return cur.rowcount


def ping() -> bool:
    """Lightweight DB connectivity check for health probes."""
    con = connect()
    try:
        row = con.execute("SELECT 1 AS ok").fetchone()
        return bool(row and row["ok"] == 1)
    finally:
        close(con)


def record_stripe_webhook_event(event_id: str, event_type: str) -> bool:
    """Insert Stripe event id; return False if already processed."""
    if not event_id:
        return True
    with tx() as con:
        try:
            con.execute(
                "INSERT INTO stripe_webhook_events (event_id, event_type) VALUES (?, ?)",
                (event_id, event_type),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def cap_alert_already_sent(tenant_id: str, period: str, alert_kind: str) -> bool:
    con = connect()
    try:
        row = con.execute(
            "SELECT 1 FROM cap_alert_log WHERE tenant_id=? AND period=? AND alert_kind=?",
            (tenant_id, period, alert_kind),
        ).fetchone()
        return row is not None
    finally:
        close(con)


def record_cap_alert(tenant_id: str, period: str, alert_kind: str) -> None:
    with tx() as con:
        con.execute(
            """INSERT OR IGNORE INTO cap_alert_log (tenant_id, period, alert_kind)
               VALUES (?, ?, ?)""",
            (tenant_id, period, alert_kind),
        )


def mise_dedup_key(mise_gallery_id: int, client_id: str | None = None) -> str:
    if client_id:
        return f"mise:gallery:{mise_gallery_id}:client:{client_id}"
    return f"mise:gallery:{mise_gallery_id}"


def get_mise_analyze_ledger(dedup_key: str) -> dict | None:
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM mise_analyze_ledger WHERE dedup_key=?",
            (dedup_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        close(con)


def upsert_mise_analyze_ledger(
    *,
    dedup_key: str,
    mise_gallery_id: int,
    client_id: str | None,
    status: str,
    run_id: int | None = None,
    job_id: str | None = None,
    folder_fingerprint: str | None = None,
) -> None:
    with tx() as con:
        con.execute(
            """INSERT INTO mise_analyze_ledger
               (dedup_key, mise_gallery_id, client_id, run_id, job_id, status,
                folder_fingerprint, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(dedup_key) DO UPDATE SET
                 run_id=COALESCE(excluded.run_id, mise_analyze_ledger.run_id),
                 job_id=COALESCE(excluded.job_id, mise_analyze_ledger.job_id),
                 status=excluded.status,
                 folder_fingerprint=COALESCE(
                     excluded.folder_fingerprint, mise_analyze_ledger.folder_fingerprint
                 ),
                 updated_at=datetime('now')""",
            (dedup_key, mise_gallery_id, client_id, run_id, job_id, status, folder_fingerprint),
        )


def enqueue_dead_letter_callback(
    *,
    idempotency_key: str,
    gallery_id: int | None,
    run_id: int | None,
    payload: str,
    last_status: str | None = None,
    last_error: str | None = None,
) -> None:
    """Persist an undelivered structured callback so it is never lost. Keyed on
    the idempotency key, so a re-dead-letter of the same (gallery, run) updates the
    existing row (bumps attempts) rather than duplicating it."""
    with tx() as con:
        con.execute(
            """INSERT INTO callback_outbox
               (idempotency_key, gallery_id, run_id, payload, attempts, last_status, last_error, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, datetime('now'))
               ON CONFLICT(idempotency_key) DO UPDATE SET
                 attempts = callback_outbox.attempts + 1,
                 payload = excluded.payload,
                 last_status = excluded.last_status,
                 last_error = excluded.last_error,
                 updated_at = datetime('now')""",
            (idempotency_key, gallery_id, run_id, payload, last_status, last_error),
        )


def list_dead_letter_callbacks(limit: int = 50) -> list[dict]:
    con = connect()
    try:
        rows = con.execute(
            "SELECT * FROM callback_outbox ORDER BY created_at ASC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        close(con)


def bump_dead_letter_attempt(idempotency_key: str, *, last_status: str | None, last_error: str | None) -> None:
    with tx() as con:
        con.execute(
            """UPDATE callback_outbox
               SET attempts = attempts + 1, last_status = ?, last_error = ?, updated_at = datetime('now')
               WHERE idempotency_key = ?""",
            (last_status, last_error, idempotency_key),
        )


def resolve_dead_letter_callback(idempotency_key: str) -> None:
    """Remove a callback from the outbox once it is delivered (or a no-op)."""
    with tx() as con:
        con.execute("DELETE FROM callback_outbox WHERE idempotency_key = ?", (idempotency_key,))


def dead_letter_callback_count() -> int:
    con = connect()
    try:
        row = con.execute("SELECT COUNT(*) AS n FROM callback_outbox").fetchone()
        return int(row["n"]) if row else 0
    finally:
        close(con)


def list_preference_clients(*, tenant_id: str | None = None) -> list[dict]:
    """Distinct client_ids with their latest preference row (homelab UI)."""
    tenant_id = _resolve_tenant_scope(tenant_id)
    con = connect()
    try:
        if tenant_id:
            rows = con.execute(
                """SELECT client_id, style, prefs, updated_at FROM preferences
                   WHERE tenant_id=? AND client_id IS NOT NULL AND client_id != ''
                   ORDER BY updated_at DESC""",
                (tenant_id,),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT client_id, style, prefs, updated_at FROM preferences
                   WHERE client_id IS NOT NULL AND client_id != ''
                   ORDER BY updated_at DESC"""
            ).fetchall()
        seen: set[str] = set()
        out: list[dict] = []
        for row in rows:
            cid = row["client_id"]
            if cid in seen:
                continue
            seen.add(cid)
            prefs = _json_or(row["prefs"], {})
            boosts = prefs.get("keyword_boosts") or []
            out.append(
                {
                    "client_id": cid,
                    "style": row["style"] or prefs.get("style"),
                    "updated_at": row["updated_at"],
                    "keyword_boosts": boosts[:5],
                    "shot_type_preference": prefs.get("shot_type_preference"),
                    "culling_bias": prefs.get("culling_bias"),
                }
            )
        return out
    finally:
        close(con)
