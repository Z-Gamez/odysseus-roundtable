"""SQLite persistence for SAW round-table runs (the evidence/audit trail).

Deliberately standalone: uses stdlib sqlite3 against `data/saw.db` rather than
Odysseus's SQLAlchemy models, so the harness adds tables without touching the
host app's schema or migrations. One short-lived connection per call keeps it
safe to use from the orchestrator's asyncio tasks.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _data_dir() -> Path:
    # Honor an explicit override, else use <odysseus_root>/data (this file lives
    # at <root>/src/saw/store.py), else fall back to ./data.
    env = os.environ.get("ODYSSEUS_DATA_DIR")
    if env:
        return Path(env)
    root = Path(__file__).resolve().parents[2]
    cand = root / "data"
    return cand if cand.exists() else Path("data")


def _db_path() -> str:
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return str(d / "saw.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_INITED = False


def init_db() -> None:
    global _INITED
    if _INITED:
        return
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS saw_runs (
                run_id      TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                description TEXT,
                acceptance  TEXT,
                workspace   TEXT,
                owner       TEXT,
                status      TEXT NOT NULL DEFAULT 'running',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saw_steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                idx         INTEGER NOT NULL,
                role        TEXT NOT NULL,
                iteration   INTEGER NOT NULL DEFAULT 1,
                model       TEXT,
                purpose     TEXT,
                output      TEXT,
                gate        TEXT,
                verdict     TEXT,
                created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_saw_steps_run ON saw_steps(run_id, idx);
            """
        )
    _INITED = True


def create_run(run_id: str, title: str, description: str, acceptance: str,
               workspace: str, owner: str) -> None:
    init_db()
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO saw_runs "
            "(run_id, title, description, acceptance, workspace, owner, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, title, description, acceptance, workspace, owner, "running", now, now),
        )


def set_run_status(run_id: str, status: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE saw_runs SET status=?, updated_at=? WHERE run_id=?",
            (status, time.time(), run_id),
        )


def add_step(run_id: str, idx: int, role: str, iteration: int, model: str,
             purpose: str, output: str, gate: Optional[str] = None,
             verdict: Optional[str] = None) -> int:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO saw_steps "
            "(run_id, idx, role, iteration, model, purpose, output, gate, verdict, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, idx, role, iteration, model, purpose, output, gate, verdict, time.time()),
        )
        return int(cur.lastrowid)


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM saw_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        steps = conn.execute(
            "SELECT * FROM saw_steps WHERE run_id=? ORDER BY idx ASC, id ASC", (run_id,)
        ).fetchall()
        run["steps"] = [dict(s) for s in steps]
        return run


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, title, status, owner, created_at, updated_at "
            "FROM saw_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
