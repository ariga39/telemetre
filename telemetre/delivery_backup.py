"""Online SQLite backup, verification and restore for the centre database.

The centre database is a single SQLite file shared by the agy-usage and quota
schemas.  A backup therefore uses SQLite's online backup API: it takes a
transactionally consistent snapshot without stopping collectors or reporters.
Every snapshot is checked with ``PRAGMA integrity_check`` before it is moved
into place.

Restore is deliberately conservative.  It verifies the snapshot first and only
installs it at a new path with an atomic no-clobber link.  It never replaces or
renames a live database or its sidecars; the operator stops writers, restores
beside the live file and switches services to the restored path.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class BackupError(RuntimeError):
    """A backup or restore could not be completed safely."""


def _utc_stamp(moment: datetime | None = None) -> str:
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%S.%fZ")


def _sqlite_uri(path: Path, *, mode: str) -> str:
    return f"{path.resolve().as_uri()}?mode={mode}"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise BackupError(f"database does not exist: {path}")
    try:
        conn = sqlite3.connect(_sqlite_uri(path, mode="ro"), uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open database read-only: {exc}") from exc


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]


def _integrity_check(conn: sqlite3.Connection) -> str:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    messages = [str(row[0]) for row in rows]
    if messages != ["ok"]:
        raise BackupError("integrity_check failed: " + "; ".join(messages[:5]))
    return "ok"


def _foreign_key_check(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    if rows:
        raise BackupError(f"foreign_key_check found {len(rows)} violation(s)")


def _row_counts(conn: sqlite3.Connection, tables: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        try:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        except sqlite3.Error as exc:
            raise BackupError(f"cannot count table {table!r}: {exc}") from exc
    return counts


def verify_backup(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Open a snapshot read-only and prove that it is structurally sound."""

    backup_path = Path(path).expanduser()
    conn = _connect_readonly(backup_path)
    try:
        integrity = _integrity_check(conn)
        _foreign_key_check(conn)
        tables = _tables(conn)
        counts = _row_counts(conn, tables)
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    except sqlite3.Error as exc:
        raise BackupError(f"cannot verify backup: {exc}") from exc
    finally:
        conn.close()
    return {
        "ok": True,
        "path": str(backup_path),
        "bytes": backup_path.stat().st_size,
        "integrity": integrity,
        "tables": tables,
        "row_counts": counts,
        "page_size": page_size,
        "page_count": page_count,
    }


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_via_sqlite(source: Path, destination: Path) -> None:
    src = _connect_readonly(source)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
        dst.commit()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite backup failed: {exc}") from exc
    finally:
        dst.close()
        src.close()


def _unique_snapshot_path(directory: Path, prefix: str, moment: datetime | None) -> Path:
    base = f"{prefix}-{_utc_stamp(moment)}"
    candidate = directory / f"{base}.sqlite3"
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = directory / f"{base}-{suffix}.sqlite3"
    return candidate


def online_backup(
    source_db: str | os.PathLike[str],
    destination_dir: str | os.PathLike[str],
    *,
    prefix: str = "telemetre-center",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Take a verified snapshot and install it atomically."""

    source = Path(source_db).expanduser()
    directory = Path(destination_dir).expanduser()
    if not source.is_file():
        raise BackupError(f"source database does not exist: {source}")
    if not prefix or "/" in prefix or prefix in {".", ".."}:
        raise BackupError("prefix must be a non-empty file name component")
    directory.mkdir(parents=True, exist_ok=True)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    final_path = _unique_snapshot_path(directory, prefix, moment)
    temp_path = directory / f".{final_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        _copy_via_sqlite(source, temp_path)
        metadata = verify_backup(temp_path)
        _fsync_file(temp_path)
        os.replace(temp_path, final_path)
        _fsync_dir(directory)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise

    return {
        "ok": True,
        "source": str(source),
        "path": str(final_path),
        "created_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bytes": final_path.stat().st_size,
        "integrity": metadata["integrity"],
        "tables": metadata["tables"],
        "row_counts": metadata["row_counts"],
    }


def restore_backup(
    backup: str | os.PathLike[str],
    target_db: str | os.PathLike[str],
) -> dict[str, Any]:
    """Restore into a new path, atomically refusing to clobber anything.

    The verified snapshot is copied to a temporary file beside the target and
    then hard-linked into place.  ``link`` fails atomically if the target
    appeared meanwhile, so concurrent restores have exactly one winner and an
    existing database is preserved.  Operators choose a new path and switch
    services to it; this public path never replaces a live database.
    """

    snapshot = Path(backup).expanduser()
    target = Path(target_db).expanduser()
    if snapshot.resolve() == target.resolve():
        raise BackupError("backup and restore target must be different files")
    metadata = verify_backup(snapshot)
    if target.exists():
        raise BackupError(f"restore target already exists: {target}; choose a new path")
    target.parent.mkdir(parents=True, exist_ok=True)

    temp_path = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.restore"
    linked = False
    try:
        _copy_via_sqlite(snapshot, temp_path)
        restored_metadata = verify_backup(temp_path)
        _fsync_file(temp_path)
        try:
            os.link(temp_path, target)
            linked = True
        except FileExistsError:
            raise BackupError(
                f"restore target appeared during restore: {target}; choose a new path"
            ) from None
        _fsync_dir(target.parent)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        if linked:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    return {
        "ok": True,
        "backup": str(snapshot),
        "target": str(target),
        "integrity": restored_metadata["integrity"],
        "tables": restored_metadata["tables"],
        "row_counts": restored_metadata["row_counts"],
        "backup_bytes": metadata["bytes"],
    }


__all__ = [
    "BackupError",
    "online_backup",
    "restore_backup",
    "verify_backup",
]
