"""Forced-command receiver for authenticated SSH delivery.

The server-side command is installed per SSH public key, for example::

    command="/usr/bin/python3 -m telemetre.delivery receive \
             --bindings /etc/telemetre/bindings.json \
             --binding-id alpha-usage \
             --center-db /var/lib/telemetre/center.sqlite3",no-pty,no-port-forwarding \
      ssh-ed25519 AAAA...

Nothing in the batch chooses machine, account, provider or kind: those come
from the binding selected by the key.  The receiver reads a strictly bounded
body, commits, and only then writes an acknowledgement.  Any refusal is
reported as JSON on stdout with a non-zero exit status.
"""

from __future__ import annotations

import fcntl
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from .delivery_bindings import (
    Binding,
    BindingError,
    KIND_AGY_USAGE,
    KIND_QUOTA_SAMPLE,
    load_bindings,
    require_binding,
)
from .delivery_quota import DeliveryQuotaError, ingest_batch, parse_batch
from .protocol import IngestContext, ProtocolError
from .receiver import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, receive




class DeliveryReceiveError(RuntimeError):
    """A bounded receive request was refused."""


@contextmanager
def _centre_commit_lock(center_db: str | Path, timeout: float = 5.0) -> Iterator[None]:
    """Serialize centre commits across concurrent forced-command processes.

    The sibling SQLite helper sets ``journal_mode=WAL`` on every connection.
    SQLite can return an immediate busy error for that pragma while another
    connection is committing, bypassing busy_timeout.  A short local file lock
    keeps concurrent submissions deterministic; the kernel releases it if a
    receiver dies.
    """

    target = str(center_db)
    if target == ":memory:":
        yield
        return
    lock_path = Path(target + ".receive.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
    except OSError as exc:
        raise DeliveryReceiveError(f"cannot open centre receive lock: {exc}") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeliveryReceiveError("centre is busy; retry later") from None
                time.sleep(0.01)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def read_bounded(stream: BinaryIO, limit: int) -> bytes:
    if limit < 1:
        raise DeliveryReceiveError("max_bytes must be positive")
    data = stream.read(limit + 1)
    if not isinstance(data, (bytes, bytearray)):
        raise DeliveryReceiveError("stdin did not produce bytes")
    if len(data) > limit:
        raise DeliveryReceiveError(f"request body exceeds {limit} bytes")
    return bytes(data)


def receive_bytes(
    center_db: str | Path,
    bindings: Mapping[str, Binding],
    binding_id: str,
    raw: bytes,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    binding = require_binding(bindings, binding_id)
    if len(raw) > max_bytes:
        raise DeliveryReceiveError(f"request body exceeds {max_bytes} bytes")
    with _centre_commit_lock(center_db):
        return _receive_locked(
            center_db,
            binding,
            raw,
            max_rows=max_rows,
            max_bytes=max_bytes,
        )


def _receive_locked(
    center_db: str | Path,
    binding: Binding,
    raw: bytes,
    *,
    max_rows: int,
    max_bytes: int,
) -> dict[str, Any]:
    if binding.kind == KIND_AGY_USAGE:
        context = IngestContext(
            machine=binding.machine,
            account=binding.account,
            kind=KIND_AGY_USAGE,
        )
        try:
            ack = receive(
                center_db,
                context,
                raw,
                max_rows=max_rows,
                max_bytes=max_bytes,
            )
        except ProtocolError:
            raise
        ack["binding_id"] = binding.binding_id
        return ack

    if binding.kind == KIND_QUOTA_SAMPLE:
        try:
            observations = parse_batch(raw, binding)
            counts = ingest_batch(center_db, observations)
        except (DeliveryQuotaError, ValueError) as exc:
            raise DeliveryReceiveError(str(exc)) from exc
        return {
            "ok": True,
            "kind": KIND_QUOTA_SAMPLE,
            "binding_id": binding.binding_id,
            "machine": binding.machine,
            "account": binding.account,
            "provider": binding.provider,
            "accepted": len(observations),
            "stored": counts["stored"],
            "duplicates": counts["duplicates"],
            "conflicts": 0,
        }

    raise DeliveryReceiveError(f"unsupported binding kind: {binding.kind!r}")


def receive_stdin(
    center_db: str | Path,
    binding_path: str | Path,
    binding_id: str,
    *,
    stdin: BinaryIO | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    stream = stdin if stdin is not None else sys.stdin.buffer
    bindings = load_bindings(binding_path)
    raw = read_bounded(stream, max_bytes)
    return receive_bytes(
        center_db,
        bindings,
        binding_id,
        raw,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )


def public_error(exc: BaseException) -> str:
    """Keep the client error useful without echoing payload or local paths."""

    if isinstance(exc, (DeliveryReceiveError, BindingError, ProtocolError)):
        text = str(exc)
    else:
        text = f"receiver failed ({type(exc).__name__})"
    text = " ".join(text.split())
    return text[:500]


__all__ = [
    "DeliveryReceiveError",
    "public_error",
    "read_bounded",
    "receive_bytes",
    "receive_stdin",
]
