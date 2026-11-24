"""SQLite schemas.

The client database is a durable spool; the centre database is the only copy
used by reports.  Both keep collector/scan diagnostics alongside facts so a
run with no new steps can still report health.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

CLIENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_state (
    path TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    dev INTEGER NOT NULL,
    ino INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    has_partial INTEGER NOT NULL DEFAULT 0,
    last_scan_run TEXT NOT NULL,
    last_scanned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    conversation_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    digest TEXT NOT NULL,
    event_us INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    cache_read_tokens INTEGER,
    output_tokens INTEGER,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (conversation_id, step_index)
);

CREATE TABLE IF NOT EXISTS pending (
    pending_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_error TEXT,
    UNIQUE (conversation_id, step_index, digest)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    collector_version TEXT,
    files_seen INTEGER NOT NULL,
    files_read INTEGER NOT NULL,
    files_skipped INTEGER NOT NULL,
    partial_files INTEGER NOT NULL,
    bad_lines INTEGER NOT NULL,
    missing_count_steps INTEGER NOT NULL,
    new_facts INTEGER NOT NULL,
    changed_facts INTEGER NOT NULL,
    pending_backlog INTEGER NOT NULL,
    source_missing INTEGER NOT NULL,
    source_errors INTEGER NOT NULL,
    roots_json TEXT NOT NULL
);
"""

CENTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    account TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    event_us INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    cache_read_tokens INTEGER,
    output_tokens INTEGER,
    status TEXT NOT NULL CHECK (status IN ('ok', 'ambiguous', 'conflict')),
    machine TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account, conversation_id, step_index)
);

CREATE TABLE IF NOT EXISTS observations (
    account TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    machine TEXT NOT NULL,
    digest TEXT NOT NULL,
    event_us INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    cache_read_tokens INTEGER,
    output_tokens INTEGER,
    collector_version TEXT,
    received_at TEXT NOT NULL,
    PRIMARY KEY (account, conversation_id, step_index, machine, digest)
);

CREATE TABLE IF NOT EXISTS event_sources (
    account TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    machine TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (account, conversation_id, step_index, machine)
);

CREATE TABLE IF NOT EXISTS machine_status (
    machine TEXT NOT NULL,
    account TEXT NOT NULL,
    kind TEXT NOT NULL,
    last_run_id TEXT,
    last_success_at TEXT,
    last_scan_json TEXT,
    collector_version TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (machine, account, kind)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    target = str(path)
    if target != ":memory:":
        Path(target).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_client_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CLIENT_SCHEMA)


def init_center_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CENTER_SCHEMA)
