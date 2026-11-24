"""Versioned wire protocol and fact canonicalisation.

Identity (machine, account, kind) is supplied by the authenticated caller, not
by a fact row.  The receiver therefore cannot be made to write another
machine's data by editing the payload.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .timeutil import parse_rfc3339, to_us, utc_text

PROTOCOL_VERSION = 1
KIND_AGY_USAGE = "agy_usage"
COUNT_FIELDS = ("input_tokens", "cache_read_tokens", "output_tokens")
MAX_SQLITE_INT = 2**63 - 1
MIN_SQLITE_INT = -(2**63)
SCAN_INT_FIELDS = (
    "files_seen",
    "files_read",
    "files_skipped",
    "partial_files",
    "bad_lines",
    "missing_count_steps",
    "new_facts",
    "changed_facts",
    "pending_backlog",
    "source_missing",
    "source_errors",
)


class ProtocolError(ValueError):
    """The batch is malformed or violates the bounded receive contract."""


@dataclass(frozen=True)
class IngestContext:
    """Identity resolved by the transport (the future SSH receiver)."""

    machine: str
    account: str
    kind: str = KIND_AGY_USAGE


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{field} must be a non-empty string")
    return value.strip()


def _count(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{field} must be a non-negative integer or null")
    if value < 0 or value > MAX_SQLITE_INT:
        raise ProtocolError(f"{field} must fit a non-negative SQLite integer")
    return value


def normalize_fact(raw: Any, index: int = 0) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProtocolError(f"facts[{index}] must be an object")
    conversation_id = _require_text(raw.get("conversation_id"), "conversation_id")
    step_index = raw.get("step_index")
    if (
        isinstance(step_index, bool)
        or not isinstance(step_index, int)
        or step_index < 0
        or step_index > MAX_SQLITE_INT
    ):
        raise ProtocolError("step_index must fit a non-negative SQLite integer")
    model = _require_text(raw.get("model"), "model")
    event_text = _require_text(raw.get("event_time"), "event_time")
    event_us = to_us(parse_rfc3339(event_text))
    if event_us < MIN_SQLITE_INT or event_us > MAX_SQLITE_INT:
        raise ProtocolError("event_time is outside the SQLite integer range")
    fact = {
        "conversation_id": conversation_id,
        "step_index": step_index,
        "event_us": event_us,
        "event_time": utc_text(event_us),
        "model": model,
    }
    for field in COUNT_FIELDS:
        fact[field] = _count(raw.get(field), field)
    return fact


def fact_digest(fact: dict[str, Any]) -> str:
    """Canonical content digest, independent of machine/account identity."""
    canonical = {
        "conversation_id": fact["conversation_id"],
        "step_index": int(fact["step_index"]),
        "event_us": int(fact["event_us"]),
        "model": fact["model"],
    }
    for field in COUNT_FIELDS:
        canonical[field] = fact[field]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_scan(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtocolError("scan must be an object or null")
    completed_text = _require_text(raw.get("completed_at"), "scan.completed_at")
    completed_us = to_us(parse_rfc3339(completed_text))
    scan: dict[str, Any] = {
        "completed_at": utc_text(completed_us),
        "run_id": _require_text(raw.get("run_id"), "scan.run_id"),
    }
    for field in SCAN_INT_FIELDS:
        value = raw.get(field, 0)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_SQLITE_INT
        ):
            raise ProtocolError(f"scan.{field} must fit a non-negative SQLite integer")
        scan[field] = value
    version = raw.get("collector_version")
    if version is not None and not isinstance(version, str):
        raise ProtocolError("scan.collector_version must be a string or null")
    scan["collector_version"] = version
    return scan


def validate_batch(
    payload: Any,
    context: IngestContext,
    *,
    max_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        raise ProtocolError("batch must be a JSON object")
    if payload.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {payload.get('protocol')!r}")
    if payload.get("kind") != context.kind:
        raise ProtocolError(f"batch kind {payload.get('kind')!r} does not match bound kind")
    facts_raw = payload.get("facts")
    if not isinstance(facts_raw, list):
        raise ProtocolError("facts must be a list")
    if len(facts_raw) > max_rows:
        raise ProtocolError(f"batch has {len(facts_raw)} facts, limit is {max_rows}")
    facts = [normalize_fact(raw, index) for index, raw in enumerate(facts_raw)]
    scan = normalize_scan(payload.get("scan"))
    collector_version = payload.get("collector_version")
    if collector_version is not None and not isinstance(collector_version, str):
        raise ProtocolError("collector_version must be a string or null")
    return facts, scan, collector_version


def encode_batch(
    facts: list[dict[str, Any]],
    scan: dict[str, Any] | None,
    collector_version: str | None,
) -> bytes:
    wire_facts = [
        {
            "conversation_id": fact["conversation_id"],
            "step_index": int(fact["step_index"]),
            "event_time": fact["event_time"],
            "model": fact["model"],
            "input_tokens": fact.get("input_tokens"),
            "cache_read_tokens": fact.get("cache_read_tokens"),
            "output_tokens": fact.get("output_tokens"),
        }
        for fact in facts
    ]
    payload = {
        "protocol": PROTOCOL_VERSION,
        "kind": KIND_AGY_USAGE,
        "collector_version": collector_version,
        "scan": scan,
        "facts": wire_facts,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
