"""SQLite persistence: connection handling, ordered migrations and deterministic seed."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import BASE_DIR

DB_PATH = Path(BASE_DIR / "infrascan.db")

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE assets (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        asset_type TEXT NOT NULL,
        region TEXT NOT NULL DEFAULT '',
        latitude REAL,
        longitude REAL,
        material TEXT NOT NULL DEFAULT '',
        installed_year INTEGER,
        last_inspection TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        is_sample INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE inspections (
        id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
        title TEXT NOT NULL DEFAULT '',
        prompts TEXT NOT NULL DEFAULT '[]',
        media_kind TEXT NOT NULL DEFAULT 'image',
        source_name TEXT NOT NULL DEFAULT '',
        source_path TEXT NOT NULL DEFAULT '',
        pixels_per_meter REAL,
        status TEXT NOT NULL DEFAULT 'queued',
        stage TEXT NOT NULL DEFAULT 'queued',
        stage_progress REAL NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT 'mock',
        sam_engine TEXT NOT NULL DEFAULT '',
        spark_engine TEXT NOT NULL DEFAULT '',
        frames_total INTEGER NOT NULL DEFAULT 0,
        frames_done INTEGER NOT NULL DEFAULT 0,
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        warnings TEXT NOT NULL DEFAULT '[]',
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        idempotency_key TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        updated_at REAL NOT NULL
    );
    CREATE UNIQUE INDEX idx_inspections_idempotency ON inspections(idempotency_key)
        WHERE idempotency_key IS NOT NULL;

    CREATE TABLE frames (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inspection_id TEXT NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
        frame_index INTEGER NOT NULL,
        source_time_ms INTEGER NOT NULL DEFAULT 0,
        path TEXT NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        enhancements TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL,
        UNIQUE (inspection_id, frame_index)
    );

    CREATE TABLE findings (
        id TEXT PRIMARY KEY,
        inspection_id TEXT NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
        asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
        source_frame INTEGER NOT NULL DEFAULT 0,
        concept TEXT NOT NULL DEFAULT '',
        prompt TEXT NOT NULL DEFAULT '',
        box TEXT NOT NULL DEFAULT '[]',
        mask TEXT NOT NULL DEFAULT '{}',
        source_size TEXT NOT NULL DEFAULT '[]',
        confidence REAL NOT NULL DEFAULT 0,
        agreement REAL NOT NULL DEFAULT 0,
        corroborated_by TEXT NOT NULL DEFAULT '[]',
        detections INTEGER NOT NULL DEFAULT 1,
        frames_seen TEXT NOT NULL DEFAULT '[]',
        area_m2 REAL,
        severity TEXT NOT NULL DEFAULT 'Unknown',
        priority TEXT NOT NULL DEFAULT 'P2',
        cause TEXT NOT NULL DEFAULT '',
        analysis TEXT NOT NULL DEFAULT '',
        consequences TEXT NOT NULL DEFAULT '[]',
        recommendations TEXT NOT NULL DEFAULT '[]',
        references_doc TEXT NOT NULL DEFAULT '[]',
        uncertainties TEXT NOT NULL DEFAULT '[]',
        needs_human_review INTEGER NOT NULL DEFAULT 0,
        reasoning_done INTEGER NOT NULL DEFAULT 0,
        detection_engine TEXT NOT NULL DEFAULT '',
        reasoning_engine TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE work_orders (
        id TEXT PRIMARY KEY,
        inspection_id TEXT NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
        asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
        finding_ids TEXT NOT NULL DEFAULT '[]',
        title TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '{}',
        priority TEXT NOT NULL DEFAULT 'P2',
        risk_rationale TEXT NOT NULL DEFAULT '',
        recommended_action TEXT NOT NULL DEFAULT '[]',
        assigned_team TEXT NOT NULL DEFAULT '',
        assignee TEXT NOT NULL DEFAULT '',
        due_date TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft',
        source_mode TEXT NOT NULL DEFAULT 'mock',
        created_by TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE work_order_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        work_order_id TEXT NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
        actor TEXT NOT NULL DEFAULT 'system',
        action TEXT NOT NULL,
        from_status TEXT NOT NULL DEFAULT '',
        to_status TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    );

    CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inspection_id TEXT NOT NULL DEFAULT '',
        asset_id TEXT NOT NULL DEFAULT '',
        agent TEXT NOT NULL DEFAULT '',
        phase TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    );

    CREATE TABLE job_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inspection_id TEXT NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        payload TEXT NOT NULL,
        created_at REAL NOT NULL
    );

    CREATE TABLE artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inspection_id TEXT NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        path TEXT NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
        bytes INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        UNIQUE (inspection_id, name)
    );

    CREATE TABLE provider_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        configured INTEGER NOT NULL DEFAULT 0,
        verified_ok INTEGER NOT NULL DEFAULT 0,
        status_code INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT 'mock',
        created_at REAL NOT NULL
    );

    CREATE INDEX idx_inspections_asset ON inspections(asset_id, created_at);
    CREATE INDEX idx_findings_inspection ON findings(inspection_id);
    CREATE INDEX idx_findings_severity ON findings(severity, status);
    CREATE INDEX idx_work_orders_status ON work_orders(status, priority);
    CREATE INDEX idx_audit_inspection ON audit_events(inspection_id, id);
    CREATE INDEX idx_job_events ON job_events(inspection_id, seq);
    """,
}


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """One short-lived connection per call: safe across threads and workers."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def init_db() -> int:
    """Apply any pending migrations. Idempotent; never destroys existing rows."""
    with connect() as connection:
        current = version(connection)
        for target in sorted(MIGRATIONS):
            if target <= current:
                continue
            connection.executescript(MIGRATIONS[target])
            connection.execute(f"PRAGMA user_version = {target}")
        return version(connection)


def dump(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def load(value: str | None, fallback: object = None) -> object:
    if not value:
        return fallback if fallback is not None else []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback if fallback is not None else []
