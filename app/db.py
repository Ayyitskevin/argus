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
    ]:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typ}")
        except sqlite3.OperationalError:
            pass
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


def create_run(source: str | None, model: str, project_id: str | None = None) -> int:
    with tx() as con:
        cur = con.execute(
            "INSERT INTO analysis_runs (source, model, project_id) VALUES (?, ?, ?)",
            (source, model, project_id),
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
                alt_text, description, suggested_iptc, raw_response)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        return int(cur.lastrowid)


def get_run(run_id: int) -> sqlite3.Row | None:
    con = connect()
    try:
        return con.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
    finally:
        close(con)


def get_photo_image_path(photo_id: int) -> str | None:
    con = connect()
    try:
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


def list_recent_runs(limit: int = 20, *, include_archived: bool = False) -> list[sqlite3.Row]:
    con = connect()
    try:
        if include_archived:
            query = "SELECT * FROM analysis_runs ORDER BY id DESC LIMIT ?"
            return con.execute(query, (limit,)).fetchall()
        return con.execute(
            "SELECT * FROM analysis_runs WHERE archived_at IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
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


def get_full_run(run_id: int) -> dict | None:
    run = get_run(run_id)
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
) -> str:
    job_id = str(uuid.uuid4())
    with tx() as con:
        con.execute(
            """INSERT INTO jobs
               (id, status, folder, limit_, write_sidecars, sidecar_dir,
                project_id, source, model, client_id, callback_url, recursive, retry_count)
               VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
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
            ),
        )
    return job_id


def get_job(job_id: str) -> dict | None:
    con = connect()
    try:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_dict(row)
    finally:
        close(con)


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
    return get_job(job_id)


def list_jobs(limit: int = 20, status: str | None = None) -> list[sqlite3.Row]:
    con = connect()
    try:
        if status:
            return con.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
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


def get_preferences(client_id: Optional[str] = None, style: Optional[str] = None) -> dict:
    con = connect()
    try:
        if client_id:
            row = con.execute(
                "SELECT prefs FROM preferences WHERE client_id=? ORDER BY updated_at DESC LIMIT 1",
                (client_id,),
            ).fetchone()
            if row:
                return _json_or(row["prefs"], {})
        if style:
            row = con.execute(
                "SELECT prefs FROM preferences WHERE style=? ORDER BY updated_at DESC LIMIT 1",
                (style,),
            ).fetchone()
            if row:
                return _json_or(row["prefs"], {})
        return {}
    finally:
        close(con)


def set_preferences(client_id: str, prefs: dict, style: Optional[str] = None) -> int:
    prefs_json = json.dumps(prefs or {})
    with tx() as con:
        con.execute(
            "DELETE FROM preferences WHERE client_id=? AND (style IS ? OR style=?)",
            (client_id, style, style),
        )
        cur = con.execute(
            "INSERT INTO preferences (client_id, style, prefs, updated_at)"
            " VALUES (?, ?, ?, datetime('now'))",
            (client_id, style, prefs_json),
        )
        return int(cur.lastrowid)


def _client_source_pattern(client_id: str) -> str:
    return f"%client:{client_id}%"


def get_client_history_stats(client_id: str) -> dict:
    """Aggregate prior runs and photo analyses for history-based prefs (Phase 4)."""
    pattern = _client_source_pattern(client_id)
    con = connect()
    try:
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
