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
        ("jobs", "project_id", "TEXT"),
        ("jobs", "source", "TEXT"),
        ("jobs", "model", "TEXT"),
        ("jobs", "client_id", "TEXT"),
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


def list_recent_runs(limit: int = 20) -> list[sqlite3.Row]:
    con = connect()
    try:
        return con.execute(
            "SELECT * FROM analysis_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        close(con)


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
) -> str:
    job_id = str(uuid.uuid4())
    with tx() as con:
        con.execute(
            """INSERT INTO jobs
               (id, status, folder, limit_, write_sidecars, sidecar_dir,
                project_id, source, model, client_id)
               VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
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


def list_jobs(limit: int = 20) -> list[sqlite3.Row]:
    con = connect()
    try:
        return con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        close(con)


def cleanup_old_jobs(days: int = 1) -> None:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with tx() as con:
        con.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'failed') AND updated_at < ?",
            (cutoff,),
        )


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


def get_client_history_stats(client_id: str) -> dict:
    con = connect()
    try:
        rows = con.execute(
            "SELECT source FROM analysis_runs WHERE source LIKE ? ORDER BY id DESC LIMIT 20",
            (f"%client:{client_id}%",),
        ).fetchall()
        num_runs = len(rows)
        return {"num_runs": num_runs, "bias": min(0.05 * num_runs, 0.2)}
    finally:
        close(con)
