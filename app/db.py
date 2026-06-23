"""Minimal SQLite layer for argus Phase 0 (local dogfood).

Follows the short-connection + WAL spirit of mise without the full migration
machinery yet. Tables are created on first use.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT,
    model TEXT,
    photo_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS photo_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
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
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
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


def create_run(source: str | None, model: str) -> int:
    with tx() as con:
        cur = con.execute(
            "INSERT INTO analysis_runs (source, model) VALUES (?, ?)",
            (source, model),
        )
        return cur.lastrowid


def save_photo_analysis(run_id: int, data: dict) -> int:
    """data keys: image_path, width, height, keywords(list), culling(dict),
    alt_text, description, suggested_iptc(dict), raw_response(str)"""
    with tx() as con:
        cur = con.execute(
            """INSERT INTO photo_analyses
               (run_id, image_path, width, height, keywords, culling,
                alt_text, description, suggested_iptc, raw_response)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""
,
            (
                run_id,
                data["image_path"],
                data.get("width"),
                data.get("height"),
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
