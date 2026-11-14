"""SQLite storage layer.

Tables
------
readings
    Effective (highest-revision) cumulative readings per (meter_id, hour_ts).
batches
    One row per submitted batch_id, with a hash of the payload for
    idempotent replay and the stored result/rejection detail.
hourly_usage
    Published settlement rows per (meter_id, hour_start).  This is the
    only state settlement consumers read; failed batches never touch it.
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    meter_id       TEXT    NOT NULL,
    hour_ts        TEXT    NOT NULL,  -- UTC, hour-aligned, YYYY-MM-DDTHH:MM:SSZ
    cumulative_kwh REAL    NOT NULL,
    revision       INTEGER NOT NULL,
    reset_flag     INTEGER NOT NULL DEFAULT 0,
    batch_id       TEXT    NOT NULL,  -- batch that last wrote this row
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (meter_id, hour_ts)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id     TEXT    PRIMARY KEY,
    payload_hash TEXT    NOT NULL,
    status       TEXT    NOT NULL,      -- 'published' | 'rejected'
    http_status  INTEGER NOT NULL,      -- status to replay for rejected batches
    result       TEXT    NOT NULL,      -- JSON: published result or rejection detail
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS hourly_usage (
    meter_id           TEXT    NOT NULL,
    hour_start         TEXT    NOT NULL,  -- UTC, hour-aligned
    status             TEXT    NOT NULL,  -- 'settled' | 'unsettleable'
    usage_kwh          REAL,              -- NULL when unsettleable
    reason             TEXT,              -- why unsettleable
    start_revision     INTEGER,           -- revision of the reading at hour_start
    end_revision       INTEGER,           -- revision of the reading at hour_start+1h
    published_batch_id TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (meter_id, hour_start)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str) -> None:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
