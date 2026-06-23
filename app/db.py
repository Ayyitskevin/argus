"""Minimal SQLite layer for argus Phase 0 (local dogfood).

Follows the short-connection + WAL spirit of mise without the full migration
machinery yet. Tables are created on first use.
"""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT,
    model TEXT,
    photo_count INTEGER DEFAULT 0,
    project_id TEXT   -- Phase 3: for batch "entire project" grouping and mise tie-in
);

CREATE TABLE IF NOT EXISTS photo_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    shot_type TEXT,          -- e.g. hero_plate, detail_texture, wide_establishing
    keywords TEXT,           -- JSON array string
    culling TEXT,            -- JSON object
    alt_text TEXT,
    description TEXT,
    suggested_iptc TEXT,     -- JSON
    raw_response TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(run_id) REFERENCES analysis_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_photo_run ON photo_analyses(run_id);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued, running, done, failed
    folder TEXT,
    limit_ INTEGER,
    write_sidecars BOOLEAN DEFAULT 0,
    sidecar_dir TEXT,
    run_id INTEGER,
    result TEXT,  -- JSON
    error TEXT,
    project_id TEXT,  -- Phase 3: batch entire project + mise project tie-in
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

-- Phase 3 slice 4: learned preferences (per-client or per-style)
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT,
    style TEXT,
    prefs TEXT NOT NULL,  -- JSON e.g. {"keyword_boosts": [...], "shot_type_preference": [...], "culling_bias": 0.1}
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_prefs_client ON preferences(client_id, style);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    # Lightweight migrations for columns added after initial tables (Phase 0)
    for col, typ in [
        ("shot_type", "TEXT"),
        ("width", "INTEGER"),
        ("height", "INTEGER"),
    ]:
        try:
            con.execute(f"ALTER TABLE photo_analyses ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # Phase 3 project batch support
    for tbl, col in [("analysis_runs", "project_id"), ("jobs", "project_id")]:
        try:
            con.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    return con


def close(con: sqlite3.Connection):
    try:
        con.close()
    except Exception:
        pass


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


def create_run(source: str | None, model: str, project_id: str | None = None) -> int:
    with tx() as con:
        cur = con.execute(
            "INSERT INTO analysis_runs (source, model, project_id) VALUES (?, ?, ?)",
            (source, model, project_id),
        )
        return cur.lastrowid


def save_photo_analysis(run_id: int, data: dict) -> int:
    """data keys: image_path, width, height, shot_type, keywords(list), culling(dict),
    alt_text, description, suggested_iptc(dict), raw_response(str)"""
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
        return cur.lastrowid


def get_run(run_id: int) -> sqlite3.Row | None:
    con = connect()
    try:
        return con.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
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
    """Return a fully structured run dict ready for export/JSON.
    Includes parsed keywords, culling, suggested_iptc, shot_type, basename.
    """
    run = get_run(run_id)
    if not run:
        return None

    raw_photos = get_photos_for_run(run_id)
    photos = []
    for row in raw_photos:
        p = dict(row)
        for key in ("keywords", "culling", "suggested_iptc"):
            val = p.get(key)
            if val:
                try:
                    p[key] = json.loads(val)
                except Exception:
                    p[key] = {} if key != "keywords" else []
            else:
                p[key] = {} if key != "keywords" else []
        shot = p.get("shot_type") or "other"
        p["shot_type"] = str(shot).strip().lower().replace(" ", "_") or "other"
        p["basename"] = os.path.basename(str(p.get("image_path") or ""))
        photos.append(p)

    return {
        "run": dict(run),
        "photos": photos,
    }


# --- Phase 2 job queue helpers (simple, sqlite-backed) ---

import uuid

def create_job(folder: str, limit: int = 20, write_sidecars: bool = False, sidecar_dir: str | None = None, project_id: str | None = None) -> str:
    job_id = str(uuid.uuid4())
    with tx() as con:
        con.execute(
            """INSERT INTO jobs (id, status, folder, limit_, write_sidecars, sidecar_dir, project_id)
               VALUES (?, 'queued', ?, ?, ?, ?, ?)""",
            (job_id, folder, limit, int(write_sidecars), sidecar_dir, project_id),
        )
    return job_id

def get_job(job_id: str) -> dict | None:
    con = connect()
    try:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("result"):
            d["result"] = json.loads(d["result"])
        return d
    finally:
        close(con)

def update_job(job_id: str, **kwargs):
    if "result" in kwargs and kwargs["result"] is not None:
        kwargs["result"] = json.dumps(kwargs["result"])
    sets = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    with tx() as con:
        con.execute(f"UPDATE jobs SET {sets}, updated_at=datetime('now') WHERE id=?", values)

def list_jobs(limit: int = 20):
    con = connect()
    try:
        return con.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    finally:
        close(con)


def cleanup_old_jobs(days: int = 1):
    """Remove old done/failed jobs to keep DB clean."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with tx() as con:
        con.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'failed') AND updated_at < ?",
            (cutoff,),
        )


# --- Phase 3 slice 4: learned preferences (simple rule-based) ---

def get_preferences(client_id: Optional[str] = None, style: Optional[str] = None) -> dict:
    """Return prefs dict for client/style (or default empty)."""
    con = connect()
    try:
        if client_id:
            row = con.execute(
                "SELECT prefs FROM preferences WHERE client_id=? ORDER BY updated_at DESC LIMIT 1",
                (client_id,)
            ).fetchone()
            if row:
                try:
                    return json.loads(row["prefs"])
                except Exception:
                    return {}
        if style:
            row = con.execute(
                "SELECT prefs FROM preferences WHERE style=? ORDER BY updated_at DESC LIMIT 1",
                (style,)
            ).fetchone()
            if row:
                try:
                    return json.loads(row["prefs"])
                except Exception:
                    return {}
        return {}
    finally:
        close(con)


def set_preferences(client_id: str, prefs: dict, style: Optional[str] = None) -> int:
    """Upsert preferences for a client (and optional style)."""
    prefs_json = json.dumps(prefs or {})
    with tx() as con:
        # simple upsert: delete then insert (keeps tiny)
        con.execute(
            "DELETE FROM preferences WHERE client_id=? AND (style IS ? OR style=?)",
            (client_id, style, style)
        )
        cur = con.execute(
            "INSERT INTO preferences (client_id, style, prefs, updated_at) VALUES (?, ?, ?, datetime('now'))",
            (client_id, style, prefs_json)
        )
        return cur.lastrowid


def get_client_history_stats(client_id: str) -> dict:
    """Phase 4 basic history stats for learned prefs (simple count from sources).
    Scans recent runs for client: prefix.
    """
    con = connect()
    try:
        rows = con.execute(
            "SELECT source FROM analysis_runs WHERE source LIKE ? ORDER BY id DESC LIMIT 20",
            (f"%client:{client_id}%",)
        ).fetchall()
        num_runs = len(rows)
        # simplistic: return count
        return {"num_runs": num_runs, "bias": min(0.05 * num_runs, 0.2)}
    finally:
        close(con)
