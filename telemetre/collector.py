"""Local agy transcript scanner and durable SQLite spool."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .db import connect, init_client_schema
from .protocol import COUNT_FIELDS, fact_digest, normalize_fact
from .timeutil import utc_text


class CollectorError(RuntimeError):
    pass


@dataclass
class ScanCounters:
    files_seen: int = 0
    files_read: int = 0
    files_skipped: int = 0
    partial_files: int = 0
    bad_lines: int = 0
    missing_count_steps: int = 0
    new_facts: int = 0
    changed_facts: int = 0
    source_missing: bool = False
    source_errors: int = 0
    roots: list[str] = field(default_factory=list)


def _safe_scandir(path: Path) -> list[os.DirEntry[str]] | None:
    """Return directory entries, or None when traversal failed.

    ``Path.glob`` silently skips unreadable directories.  Explicit scandir
    failures are counted so a partially scanned source never looks healthy.
    A missing path is an ordinary zero-use result, not a failure.
    """
    try:
        with os.scandir(path) as entries:
            return list(entries)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        return None


def _transcript_paths(root: Path, counters: ScanCounters) -> list[Path]:
    brain_entries = _safe_scandir(root / "brain")
    if brain_entries is None:
        counters.source_errors += 1
        return []
    paths: list[Path] = []
    for conversation in brain_entries:
        try:
            if not conversation.is_dir():
                continue
        except OSError:
            counters.source_errors += 1
            continue
        system_entries = _safe_scandir(Path(conversation.path) / ".system_generated")
        if system_entries is None:
            counters.source_errors += 1
            continue
        for system_entry in system_entries:
            if system_entry.name != "logs":
                continue
            logs_entries = _safe_scandir(Path(system_entry.path))
            if logs_entries is None:
                counters.source_errors += 1
                continue
            for logs_entry in logs_entries:
                if logs_entry.name == "transcript.jsonl":
                    paths.append(Path(logs_entry.path))
    return sorted(paths)


def _complete_lines(data: bytes) -> tuple[list[bytes], bool]:
    """Split on newline and never return an unterminated trailing fragment."""
    if not data:
        return [], False
    if data.endswith(b"\n"):
        return data.split(b"\n")[:-1], False
    chunks = data.split(b"\n")
    partial = chunks.pop()
    return chunks, bool(partial)


def _parse_line(line: bytes, conversation_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return (model fact, error).  Non-model lines return (None, None).

    Sampled agy transcripts mark completed model steps with
    ``type=PLANNER_RESPONSE`` and ``status=DONE``.  Tool/user rows are not
    model events even when they carry the same step index or counters.
    """
    try:
        raw = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid json"
    if not isinstance(raw, dict):
        return None, "line is not an object"
    if raw.get("type") != "PLANNER_RESPONSE":
        return None, None
    if raw.get("status") != "DONE":
        return None, None
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        return None, "model step without model"
    if "step_index" not in raw:
        return None, "model step without step_index"
    candidate = dict(raw)
    candidate["conversation_id"] = conversation_id
    # agy stores the event instant as created_at; the wire protocol calls the
    # normalised field event_time.
    candidate["event_time"] = raw.get("created_at")
    try:
        fact = normalize_fact(candidate)
    except (ValueError, TypeError) as exc:
        return None, str(exc)
    return fact, None


def _stat_signature(stat_result: Any) -> tuple[int, int, int, int]:
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def _read_stable(transcript: Path, attempts: int = 2) -> tuple[bytes, Any, bool]:
    """Read only when the file signature is identical before and after.

    Returning ``stable=False`` means the caller must not advance its stored
    signature: part of the file may have been appended or rewritten after the
    bytes were read.
    """
    last_data = b""
    last_stat = transcript.stat()
    for _ in range(attempts):
        before = transcript.stat()
        data = transcript.read_bytes()
        after = transcript.stat()
        last_data, last_stat = data, after
        if _stat_signature(before) == _stat_signature(after):
            return data, after, True
    return last_data, last_stat, False


def _ensure_identity(conn: Any, machine: str, account: str) -> None:
    existing = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM meta WHERE key IN ('machine', 'account')")
    }
    if existing:
        if existing.get("machine") != machine or existing.get("account") != account:
            raise CollectorError(
                "client database already belongs to "
                f"{existing.get('machine')!r}/{existing.get('account')!r}"
            )
    else:
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [("machine", machine), ("account", account)],
        )


def collect(
    client_db: str | Path,
    *,
    machine: str,
    account: str,
    roots: Iterable[str | Path],
    collector_version: str = __version__,
) -> dict[str, Any]:
    """Scan roots and commit file progress, facts and pending rows atomically."""
    if not machine.strip() or not account.strip():
        raise CollectorError("machine and account must be non-empty")
    root_paths = [Path(root).expanduser() for root in roots]
    now = utc_text(datetime.now(timezone.utc))
    run_id = uuid.uuid4().hex
    counters = ScanCounters(roots=[str(root) for root in root_paths])

    conn = connect(client_db)
    try:
        init_client_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_identity(conn, machine, account)
            for root in root_paths:
                if not root.is_dir():
                    counters.source_missing = True
                    continue
                for transcript in _transcript_paths(root, counters):
                    counters.files_seen += 1
                    conversation_id = transcript.parents[2].name
                    try:
                        before = transcript.stat()
                        signature = _stat_signature(before)
                        state = conn.execute(
                            "SELECT dev, ino, size, mtime_ns, has_partial FROM file_state WHERE path = ?",
                            (str(transcript),),
                        ).fetchone()
                        unchanged = (
                            state is not None
                            and (state["dev"], state["ino"], state["size"], state["mtime_ns"])
                            == signature
                            and not state["has_partial"]
                        )
                        if unchanged:
                            counters.files_skipped += 1
                            continue

                        data, after, stable = _read_stable(transcript)
                        signature = _stat_signature(after)
                    except OSError:
                        counters.source_errors += 1
                        continue

                    lines, has_partial = _complete_lines(data)
                    counters.files_read += 1
                    if has_partial:
                        counters.partial_files += 1
                    for line in lines:
                        fact, error = _parse_line(line, conversation_id)
                        if error is not None:
                            counters.bad_lines += 1
                            continue
                        if fact is None:
                            continue
                        if any(fact[field] is None for field in COUNT_FIELDS):
                            counters.missing_count_steps += 1

                        digest = fact_digest(fact)
                        previous = conn.execute(
                            "SELECT digest FROM facts WHERE conversation_id = ? AND step_index = ?",
                            (conversation_id, fact["step_index"]),
                        ).fetchone()
                        payload = json.dumps(fact, sort_keys=True, separators=(",", ":"))
                        if previous is None:
                            conn.execute(
                                "INSERT INTO facts (conversation_id, step_index, digest, event_us, "
                                "event_time, model, input_tokens, cache_read_tokens, output_tokens, "
                                "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    conversation_id,
                                    fact["step_index"],
                                    digest,
                                    fact["event_us"],
                                    fact["event_time"],
                                    fact["model"],
                                    fact["input_tokens"],
                                    fact["cache_read_tokens"],
                                    fact["output_tokens"],
                                    now,
                                    now,
                                ),
                            )
                            conn.execute(
                                "INSERT OR IGNORE INTO pending "
                                "(conversation_id, step_index, digest, payload, enqueued_at) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (conversation_id, fact["step_index"], digest, payload, now),
                            )
                            counters.new_facts += 1
                        elif previous["digest"] != digest:
                            conn.execute(
                                "UPDATE facts SET digest = ?, event_us = ?, event_time = ?, model = ?, "
                                "input_tokens = ?, cache_read_tokens = ?, output_tokens = ?, "
                                "last_seen = ? WHERE conversation_id = ? AND step_index = ?",
                                (
                                    digest,
                                    fact["event_us"],
                                    fact["event_time"],
                                    fact["model"],
                                    fact["input_tokens"],
                                    fact["cache_read_tokens"],
                                    fact["output_tokens"],
                                    now,
                                    conversation_id,
                                    fact["step_index"],
                                ),
                            )
                            conn.execute(
                                "INSERT OR IGNORE INTO pending "
                                "(conversation_id, step_index, digest, payload, enqueued_at) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (conversation_id, fact["step_index"], digest, payload, now),
                            )
                            counters.changed_facts += 1
                        else:
                            conn.execute(
                                "UPDATE facts SET last_seen = ? WHERE conversation_id = ? AND step_index = ?",
                                (now, conversation_id, fact["step_index"]),
                            )

                    if stable:
                        conn.execute(
                            "INSERT INTO file_state (path, conversation_id, dev, ino, size, mtime_ns, "
                            "has_partial, last_scan_run, last_scanned_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(path) DO UPDATE SET conversation_id=excluded.conversation_id, "
                            "dev=excluded.dev, ino=excluded.ino, size=excluded.size, "
                            "mtime_ns=excluded.mtime_ns, has_partial=excluded.has_partial, "
                            "last_scan_run=excluded.last_scan_run, last_scanned_at=excluded.last_scanned_at",
                            (
                                str(transcript),
                                conversation_id,
                                signature[0],
                                signature[1],
                                signature[2],
                                signature[3],
                                1 if has_partial else 0,
                                run_id,
                                now,
                            ),
                        )

            pending_backlog = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            completed_at = utc_text(datetime.now(timezone.utc))
            conn.execute(
                "INSERT INTO scan_runs (run_id, started_at, completed_at, collector_version, "
                "files_seen, files_read, files_skipped, partial_files, bad_lines, "
                "missing_count_steps, new_facts, changed_facts, pending_backlog, source_missing, "
                "source_errors, roots_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    now,
                    completed_at,
                    collector_version,
                    counters.files_seen,
                    counters.files_read,
                    counters.files_skipped,
                    counters.partial_files,
                    counters.bad_lines,
                    counters.missing_count_steps,
                    counters.new_facts,
                    counters.changed_facts,
                    pending_backlog,
                    1 if counters.source_missing else 0,
                    counters.source_errors,
                    json.dumps(counters.roots),
                ),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "run_id": run_id,
        "machine": machine,
        "account": account,
        "completed_at": completed_at,
        "roots": counters.roots,
        "files_seen": counters.files_seen,
        "files_read": counters.files_read,
        "files_skipped": counters.files_skipped,
        "partial_files": counters.partial_files,
        "bad_lines": counters.bad_lines,
        "missing_count_steps": counters.missing_count_steps,
        "new_facts": counters.new_facts,
        "changed_facts": counters.changed_facts,
        "pending_backlog": pending_backlog,
        "source_missing": counters.source_missing,
        "source_errors": counters.source_errors,
        "collector_version": collector_version,
    }
