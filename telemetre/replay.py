"""Client-side bounded spool replay.

A transport is any object with ``send(batch_bytes) -> ack_dict``.  The local
implementation talks straight to :func:`telemetre.receiver.receive`; #3 can wrap
the same bytes with SSH without changing the spool or protocol.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .db import connect, init_client_schema
from .protocol import KIND_AGY_USAGE, IngestContext, encode_batch
from .receiver import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ROWS,
    receive,
)
from .timeutil import utc_text


class Transport(Protocol):
    def send(self, batch: bytes) -> dict[str, Any]: ...


class ReplayError(RuntimeError):
    """A delivery or acknowledgement failure that must not delete spool rows."""


def _validate_ack(
    ack: Any,
    *,
    accepted: int,
    kind: str,
    machine: str,
    account: str,
) -> dict[str, Any]:
    if not isinstance(ack, dict):
        raise ReplayError("receiver acknowledgement rejected: not an object")
    if ack.get("ok") is not True:
        raise ReplayError("receiver acknowledgement rejected: not acknowledged")
    for field in ("accepted", "stored", "duplicates"):
        if field not in ack:
            raise ReplayError(
                f"receiver acknowledgement rejected: {field} is missing"
            )
        value = ack[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReplayError(
                f"receiver acknowledgement rejected: {field} is not a non-negative integer"
            )
    if "conflicts" in ack:
        value = ack["conflicts"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReplayError(
                "receiver acknowledgement rejected: conflicts is not a non-negative integer"
            )
    if ack["accepted"] != accepted:
        raise ReplayError(
            "receiver acknowledgement rejected: accepted count does not match batch"
        )
    if ack["stored"] + ack["duplicates"] != ack["accepted"]:
        raise ReplayError(
            "receiver acknowledgement rejected: stored + duplicates != accepted"
        )
    expected = {"kind": kind, "machine": machine, "account": account}
    for field, value in expected.items():
        if ack.get(field) != value:
            raise ReplayError(
                f"receiver acknowledgement rejected: {field} does not match bound spool"
            )
    return ack


def spool_identity(client_db: str | Path) -> dict[str, str] | None:
    """Read the machine/account bound to a client spool."""
    conn = connect(client_db)
    try:
        init_client_schema(conn)
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key IN ('machine', 'account')"
        ).fetchall()
    finally:
        conn.close()
    values = {row["key"]: row["value"] for row in rows}
    if "machine" not in values or "account" not in values:
        return None
    return {"machine": values["machine"], "account": values["account"]}


@dataclass
class NextBatch:
    payload: bytes
    pending_ids: list[int]
    fact_count: int


class LocalCenterTransport:
    """In-process implementation of the seam #3 will replace with SSH."""

    def __init__(
        self,
        center_db: str | Path,
        context: IngestContext,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.center_db = center_db
        self.context = context
        self.max_rows = max_rows
        self.max_bytes = max_bytes

    def send(self, batch: bytes) -> dict[str, Any]:
        return receive(
            self.center_db,
            self.context,
            batch,
            max_rows=self.max_rows,
            max_bytes=self.max_bytes,
        )


def _latest_scan(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM scan_runs ORDER BY completed_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    # Local filesystem roots stay in the client database.  They are never part
    # of the wire batch or centre status.
    return {
        "run_id": row["run_id"],
        "completed_at": row["completed_at"],
        "collector_version": row["collector_version"],
        "files_seen": row["files_seen"],
        "files_read": row["files_read"],
        "files_skipped": row["files_skipped"],
        "partial_files": row["partial_files"],
        "bad_lines": row["bad_lines"],
        "missing_count_steps": row["missing_count_steps"],
        "new_facts": row["new_facts"],
        "changed_facts": row["changed_facts"],
        "pending_backlog": row["pending_backlog"],
        "source_missing": row["source_missing"],
        "source_errors": row["source_errors"],
    }


def build_next_batch(
    client_db: str | Path,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> NextBatch:
    if max_rows < 1 or max_bytes < 1:
        raise ValueError("max_rows and max_bytes must be positive")
    conn = connect(client_db)
    try:
        init_client_schema(conn)
        rows = conn.execute(
            "SELECT pending_id, payload FROM pending ORDER BY pending_id LIMIT ?",
            (max_rows,),
        ).fetchall()
        scan = _latest_scan(conn)
        collector_version = scan.get("collector_version") if scan else None
    finally:
        conn.close()

    facts = [json.loads(row["payload"]) for row in rows]
    ids = [int(row["pending_id"]) for row in rows]
    selected = len(facts)
    while True:
        payload = encode_batch(facts, scan, collector_version)
        if len(payload) <= max_bytes:
            return NextBatch(payload=payload, pending_ids=ids, fact_count=len(facts))
        if selected and len(facts) <= 1:
            raise ReplayError(
                "single fact cannot fit the byte cap; it is retained in the local spool"
            )
        if not facts:
            raise ReplayError(
                f"scan/status batch alone is {len(payload)} bytes, limit is {max_bytes}"
            )
        facts.pop()
        ids.pop()


def _mark_failed(client_db: str | Path, pending_ids: list[int], error: str) -> None:
    if not pending_ids:
        return
    now = utc_text(datetime.now(timezone.utc))
    conn = connect(client_db)
    try:
        init_client_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for pending_id in pending_ids:
                conn.execute(
                    "UPDATE pending SET attempts = attempts + 1, last_attempt_at = ?, last_error = ? "
                    "WHERE pending_id = ?",
                    (now, error, pending_id),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _delete_delivered(client_db: str | Path, pending_ids: list[int]) -> int:
    if not pending_ids:
        return 0
    conn = connect(client_db)
    try:
        init_client_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" for _ in pending_ids)
            deleted = conn.execute(
                f"DELETE FROM pending WHERE pending_id IN ({placeholders})",
                pending_ids,
            ).rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return deleted


def _pending_remaining(client_db: str | Path) -> int:
    conn = connect(client_db)
    try:
        init_client_schema(conn)
        return int(conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
    finally:
        conn.close()


def replay_pending(
    client_db: str | Path,
    transport: Transport,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Send bounded batches, deleting rows only after a matching acknowledgement."""
    identity = spool_identity(client_db)
    if identity is None:
        raise ReplayError("client spool has no machine/account identity")
    batches = 0
    sent = 0
    deleted = 0
    duplicates = 0
    while True:
        batch = build_next_batch(client_db, max_rows=max_rows, max_bytes=max_bytes)
        try:
            ack = _validate_ack(
                transport.send(batch.payload),
                accepted=batch.fact_count,
                kind=KIND_AGY_USAGE,
                machine=identity["machine"],
                account=identity["account"],
            )
        except BaseException:
            # The ack text itself is never persisted or replayed.  Keeping the
            # rows is the only safe response to an unknown delivery outcome.
            _mark_failed(client_db, batch.pending_ids, "delivery or acknowledgement failed")
            raise
        batches += 1
        sent += batch.fact_count
        duplicates += int(ack.get("duplicates", 0))
        deleted += _delete_delivered(client_db, batch.pending_ids)
        if not batch.pending_ids:
            break
        if _pending_remaining(client_db) == 0:
            # The last real batch already carried this scan report.
            break
    return {
        "ok": True,
        "batches": batches,
        "sent_facts": sent,
        "deleted_pending": deleted,
        "duplicates": duplicates,
    }
