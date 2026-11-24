"""Bounded local receive seam.

#3 will authenticate an SSH key, derive ``IngestContext`` from its binding and
call :func:`receive` with the batch bytes.  Nothing here opens a socket or
trusts machine/account fields from the payload.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect, init_center_schema
from .protocol import (
    COUNT_FIELDS,
    KIND_AGY_USAGE,
    IngestContext,
    ProtocolError,
    fact_digest,
    validate_batch,
)
from .timeutil import parse_rfc3339, to_us, utc_text

DEFAULT_MAX_ROWS = 1000
DEFAULT_MAX_BYTES = 256 * 1024


def _merge_event(
    conn: sqlite3.Connection,
    context: IngestContext,
    fact: dict[str, Any],
    digest: str,
    collector_version: str | None,
    now: str,
) -> tuple[bool, bool]:
    """Store one observation. Return (inserted_observation, event_is_conflict)."""
    inserted = conn.execute(
        "INSERT OR IGNORE INTO observations (account, conversation_id, step_index, machine, "
        "digest, event_us, event_time, model, input_tokens, cache_read_tokens, output_tokens, "
        "collector_version, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            context.account,
            fact["conversation_id"],
            fact["step_index"],
            context.machine,
            digest,
            fact["event_us"],
            fact["event_time"],
            fact["model"],
            fact["input_tokens"],
            fact["cache_read_tokens"],
            fact["output_tokens"],
            collector_version,
            now,
        ),
    ).rowcount
    if not inserted:
        return False, False

    conn.execute(
        "INSERT INTO event_sources (account, conversation_id, step_index, machine, "
        "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account, conversation_id, step_index, machine) "
        "DO UPDATE SET last_seen = excluded.last_seen",
        (
            context.account,
            fact["conversation_id"],
            fact["step_index"],
            context.machine,
            now,
            now,
        ),
    )
    sources = conn.execute(
        "SELECT COUNT(*) FROM event_sources WHERE account = ? AND conversation_id = ? AND step_index = ?",
        (context.account, fact["conversation_id"], fact["step_index"]),
    ).fetchone()[0]

    existing = conn.execute(
        "SELECT * FROM events WHERE account = ? AND conversation_id = ? AND step_index = ?",
        (context.account, fact["conversation_id"], fact["step_index"]),
    ).fetchone()

    conflict = bool(existing is not None and existing["status"] == "conflict")
    if existing is not None:
        # The identity also fixes the model and event month.  A different model
        # or instant is conflicting evidence, never a first-seen overwrite.
        if fact["model"] != existing["model"]:
            conflict = True
        if fact["event_us"] != existing["event_us"]:
            conflict = True
    values: dict[str, int | None] = {}
    for field in COUNT_FIELDS:
        old_value = None if existing is None else existing[field]
        new_value = fact[field]
        if new_value is not None:
            if old_value is None:
                values[field] = new_value
            elif old_value != new_value:
                conflict = True
                values[field] = old_value
            else:
                values[field] = old_value
        else:
            values[field] = old_value

    status = "conflict" if conflict else ("ambiguous" if sources > 1 else "ok")
    if existing is None:
        conn.execute(
            "INSERT INTO events (account, conversation_id, step_index, event_us, event_time, "
            "model, input_tokens, cache_read_tokens, output_tokens, status, machine, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                context.account,
                fact["conversation_id"],
                fact["step_index"],
                fact["event_us"],
                fact["event_time"],
                fact["model"],
                values["input_tokens"],
                values["cache_read_tokens"],
                values["output_tokens"],
                status,
                context.machine,
                now,
                now,
            ),
        )
    else:
        conn.execute(
            "UPDATE events SET input_tokens = ?, cache_read_tokens = ?, output_tokens = ?, "
            "status = ?, updated_at = ? WHERE account = ? AND conversation_id = ? AND step_index = ?",
            (
                values["input_tokens"],
                values["cache_read_tokens"],
                values["output_tokens"],
                status,
                now,
                context.account,
                fact["conversation_id"],
                fact["step_index"],
            ),
        )
    return True, status == "conflict"


def _record_scan(
    conn: sqlite3.Connection,
    context: IngestContext,
    scan: dict[str, Any],
    collector_version: str | None,
    now: str,
) -> bool:
    existing = conn.execute(
        "SELECT last_success_at FROM machine_status WHERE machine = ? AND account = ? AND kind = ?",
        (context.machine, context.account, context.kind),
    ).fetchone()
    if existing is not None and existing["last_success_at"]:
        try:
            if to_us(parse_rfc3339(existing["last_success_at"])) > to_us(
                parse_rfc3339(scan["completed_at"])
            ):
                # A delayed/out-of-order batch must not rewind health.
                return False
        except ValueError:
            pass
    version = collector_version or scan.get("collector_version")
    conn.execute(
        "INSERT INTO machine_status (machine, account, kind, last_run_id, last_success_at, "
        "last_scan_json, collector_version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(machine, account, kind) DO UPDATE SET "
        "last_run_id=excluded.last_run_id, last_success_at=excluded.last_success_at, "
        "last_scan_json=excluded.last_scan_json, collector_version=excluded.collector_version, "
        "updated_at=excluded.updated_at",
        (
            context.machine,
            context.account,
            context.kind,
            scan["run_id"],
            scan["completed_at"],
            json.dumps(scan, sort_keys=True, separators=(",", ":")),
            version,
            now,
        ),
    )
    return True


def receive(
    center_db: str | Path,
    context: IngestContext,
    raw: bytes,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Validate, commit and only then acknowledge one bounded batch."""
    if context.kind != KIND_AGY_USAGE:
        raise ProtocolError(f"unsupported bound kind: {context.kind!r}")
    if not isinstance(raw, (bytes, bytearray)):
        raise ProtocolError("batch must be bytes")
    if len(raw) > max_bytes:
        raise ProtocolError(f"batch is {len(raw)} bytes, limit is {max_bytes}")
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError(f"invalid batch JSON: {exc}") from exc
    facts, scan, collector_version = validate_batch(payload, context, max_rows=max_rows)

    now = utc_text(datetime.now(timezone.utc))
    conn = connect(center_db)
    try:
        init_center_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored = 0
            duplicates = 0
            conflicts = 0
            for fact in facts:
                inserted, is_conflict = _merge_event(
                    conn,
                    context,
                    fact,
                    fact_digest(fact),
                    collector_version,
                    now,
                )
                if inserted:
                    stored += 1
                else:
                    duplicates += 1
                if is_conflict:
                    conflicts += 1
            if scan is not None:
                _record_scan(conn, context, scan, collector_version, now)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "ok": True,
        "kind": context.kind,
        "machine": context.machine,
        "account": context.account,
        "accepted": len(facts),
        "stored": stored,
        "duplicates": duplicates,
        "conflicts": conflicts,
    }
