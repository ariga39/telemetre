"""Behavior checks for the online backup/restore slice."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from telemetre import db
from telemetre.delivery_backup import (
    BackupError,
    online_backup,
    restore_backup,
    verify_backup,
)


def _init_center(path: Path) -> None:
    conn = db.connect(path)
    try:
        db.init_center_schema(conn)
    finally:
        conn.close()


def _insert_event(conn: sqlite3.Connection, account: str, step: int) -> None:
    now = "2026-11-01T00:00:00Z"
    conn.execute(
        "INSERT INTO events (account, conversation_id, step_index, event_us, event_time, "
        "model, input_tokens, cache_read_tokens, output_tokens, status, machine, "
        "created_at, updated_at) VALUES (?, 'conv', ?, 0, ?, 'm', 1, 2, 3, 'ok', ?, ?, ?)",
        (account, step, now, "machine-a", now, now),
    )


class OnlineBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_online_snapshot_is_consistent_while_a_writer_keeps_committing(self) -> None:
        source = self.tmp / "center.sqlite3"
        _init_center(source)
        writer = db.connect(source)
        writer.execute("BEGIN IMMEDIATE")
        _insert_event(writer, "acct", 0)
        writer.execute("COMMIT")
        stop = threading.Event()
        errors: list[BaseException] = []

        def keep_writing() -> None:
            conn = db.connect(source)
            try:
                for step in range(1, 120):
                    if stop.is_set():
                        break
                    conn.execute("BEGIN IMMEDIATE")
                    _insert_event(conn, "acct", step)
                    conn.execute("COMMIT")
                    time.sleep(0.002)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                conn.close()

        thread = threading.Thread(target=keep_writing)
        thread.start()
        try:
            result = online_backup(source, self.tmp / "snapshots", prefix="center")
        finally:
            stop.set()
            thread.join(timeout=10)
        writer.close()
        self.assertFalse(errors)
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["row_counts"]["events"], result["row_counts"]["events"])
        self.assertGreaterEqual(result["row_counts"]["events"], 1)

        restored = self.tmp / "restored.sqlite3"
        restore_backup(result["path"], restored)
        conn = db.connect(restored)
        try:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            broken = conn.execute("PRAGMA integrity_check").fetchone()[0]
            pairs = conn.execute(
                "SELECT account, step_index FROM events ORDER BY step_index"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(broken, "ok")
        self.assertEqual(count, result["row_counts"]["events"])
        self.assertEqual(len({row[1] for row in pairs}), count)

    def test_verify_reports_tables_and_rejects_a_damaged_snapshot(self) -> None:
        source = self.tmp / "center.sqlite3"
        _init_center(source)
        conn = db.connect(source)
        try:
            _insert_event(conn, "acct", 1)
        finally:
            conn.close()
        snapshot = online_backup(source, self.tmp / "snapshots")["path"]
        verified = verify_backup(snapshot)
        self.assertTrue(verified["ok"])
        self.assertIn("events", verified["tables"])
        self.assertEqual(verified["row_counts"]["events"], 1)

        damaged = self.tmp / "damaged.sqlite3"
        damaged.write_bytes(Path(snapshot).read_bytes()[:200])
        with self.assertRaises(BackupError):
            verify_backup(damaged)
        with self.assertRaises(BackupError):
            restore_backup(damaged, self.tmp / "never.sqlite3")

    def test_restore_refuses_to_overwrite_and_preserves_the_existing_target(self) -> None:
        source = self.tmp / "center.sqlite3"
        _init_center(source)
        conn = db.connect(source)
        try:
            _insert_event(conn, "old", 1)
        finally:
            conn.close()
        snapshot = online_backup(source, self.tmp / "snapshots")["path"]

        target = self.tmp / "live.sqlite3"
        _init_center(target)
        conn = db.connect(target)
        try:
            _insert_event(conn, "new", 2)
        finally:
            conn.close()

        with self.assertRaises(BackupError):
            restore_backup(snapshot, target)

        conn = db.connect(target)
        try:
            accounts = [row[0] for row in conn.execute("SELECT account FROM events")]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(accounts, ["new"])
        self.assertEqual(integrity, "ok")

    def test_concurrent_restore_to_the_same_new_path_has_one_winner(self) -> None:
        source = self.tmp / "center.sqlite3"
        _init_center(source)
        conn = db.connect(source)
        try:
            _insert_event(conn, "old", 1)
        finally:
            conn.close()
        snapshot = online_backup(source, self.tmp / "snapshots")["path"]
        target = self.tmp / "restored.sqlite3"
        successes: list[dict] = []
        failures: list[BaseException] = []

        def attempt() -> None:
            try:
                successes.append(restore_backup(snapshot, target))
            except BaseException as exc:  # one racer must lose atomically
                failures.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(len(successes), 1, failures)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], BackupError)
        conn = db.connect(target)
        try:
            accounts = [row[0] for row in conn.execute("SELECT account FROM events")]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(accounts, ["old"])
        self.assertEqual(integrity, "ok")


if __name__ == "__main__":
    unittest.main()
