"""Adapter between the quota sampler's local store and bounded delivery.

The quota slice owns sampling and the sanitized observation model.  This module
does not sample anything.  It reads already-stored observations through the
public ``iter_observations``/``read_observation`` seam, freezes only the sample
id in the delivery outbox, and sends one observation per request.  Retries use
the same sample id; acknowledgement bookkeeping never stores a second copy of
the payload.

The receiver re-validates and normalises every field at the trust boundary.
Caller-supplied display percentages, anomaly categories, reset values and error
details are not trusted as-is: inconsistent values are rejected, derived values
are recomputed, and error details are reduced to safe categories.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .delivery_bindings import Binding, KIND_QUOTA_SAMPLE
from .replay import ReplayError, validate_ack

PROTOCOL_VERSION = 1
DEFAULT_QUOTA_MAX_BYTES = 256 * 1024
OUTBOX_TABLE = "delivery_quota_outbox"

_ALLOWED_UNITS = {"fraction", "percent_used"}
_ALLOWED_ANOMALIES = {
    "missing_remaining",
    "remaining_out_of_range",
    "invalid_reset",
    "invalid_window",
}
_ALLOWED_ERROR_KINDS = {
    "spawn_error",
    "timeout",
    "invalid_json",
    "invalid_window",
    "invalid_observation",
    "not_authenticated",
    "provider_error",
    "exit_code",
    "protocol_error",
    "unrecognized_structure",
    "unsupported_provider",
}
_SAFE_ERROR_DETAIL = {
    "spawn_error": "provider could not be started",
    "timeout": "provider timed out",
    "invalid_json": "provider returned invalid JSON",
    "invalid_window": "provider window is outside the supported range",
    "invalid_observation": "provider response did not satisfy the observation contract",
    "not_authenticated": "provider requires authentication",
    "provider_error": "provider returned an error",
    "exit_code": "provider exited with an error",
    "protocol_error": "provider protocol error",
    "unrecognized_structure": "provider returned an unrecognized structure",
    "unsupported_provider": "provider is unsupported",
}
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

_OUTBOX_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {OUTBOX_TABLE} (
    sample_id       TEXT PRIMARY KEY,
    enqueued_at     TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_error      TEXT,
    delivered_at    TEXT
);
CREATE INDEX IF NOT EXISTS {OUTBOX_TABLE}_pending
    ON {OUTBOX_TABLE} (delivered_at, enqueued_at, sample_id);
"""


class DeliveryQuotaError(RuntimeError):
    """The quota stores could not be adapted or delivered safely."""


@dataclass(frozen=True)
class QuotaBatch:
    payload: bytes
    sample_id: str
    observation_count: int = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_quota():
    try:
        from . import quota as quota_module
    except Exception as exc:  # pragma: no cover - import failure is operational
        raise DeliveryQuotaError(f"quota module is unavailable: {exc}") from exc
    required = (
        "Observation",
        "WindowObservation",
        "connect_database",
        "ingest_observation",
        "iter_observations",
        "read_observation",
    )
    for name in required:
        if not hasattr(quota_module, name):
            raise DeliveryQuotaError(f"quota module does not expose {name}")
    return quota_module


def _ensure_outbox(conn: sqlite3.Connection) -> None:
    conn.executescript(_OUTBOX_SCHEMA)


def _open_outbox(quota_db: str | Path) -> sqlite3.Connection:
    quota_module = _load_quota()
    conn = quota_module.connect_database(quota_db)
    _ensure_outbox(conn)
    conn.commit()
    return conn


def _binding_identity(binding: Binding) -> tuple[str, str, str]:
    if binding.kind != KIND_QUOTA_SAMPLE:
        raise DeliveryQuotaError("binding is not a quota_sample binding")
    if not binding.provider:
        raise DeliveryQuotaError("quota binding has no provider")
    return binding.machine, binding.provider, binding.account


def sync_outbox(quota_db: str | Path, binding: Binding) -> int:
    """Record only this binding's stored observations, not other identities."""

    machine, provider, account = _binding_identity(binding)
    quota_module = _load_quota()
    inserted = 0
    conn = _open_outbox(quota_db)
    try:
        observations = quota_module.iter_observations(
            quota_db,
            provider=provider,
            account=account,
            machine=machine,
        )
        for observation in observations:
            if not _SAFE_ID.match(observation.sample_id or ""):
                raise DeliveryQuotaError("stored quota sample id is not safe")
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO {OUTBOX_TABLE} (sample_id, enqueued_at) "
                "VALUES (?, ?)",
                (observation.sample_id, _utc_now()),
            )
            inserted += int(cursor.rowcount)
            if inserted and inserted % 100 == 0:
                conn.commit()
        conn.commit()
        return inserted
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_next_batch(
    quota_db: str | Path,
    binding: Binding,
    *,
    max_bytes: int = DEFAULT_QUOTA_MAX_BYTES,
) -> QuotaBatch | None:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    machine, provider, account = _binding_identity(binding)
    quota_module = _load_quota()
    conn = _open_outbox(quota_db)
    try:
        row = conn.execute(
            f"SELECT outbox.sample_id FROM {OUTBOX_TABLE} AS outbox "
            "JOIN quota_samples AS sample ON sample.sample_id = outbox.sample_id "
            "WHERE outbox.delivered_at IS NULL AND sample.machine = ? "
            "AND sample.provider = ? AND sample.account = ? "
            "ORDER BY outbox.enqueued_at, outbox.sample_id LIMIT 1",
            (machine, provider, account),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    sample_id = str(row["sample_id"])
    observation = quota_module.read_observation(quota_db, sample_id)
    if observation is None:
        raise DeliveryQuotaError(f"pending quota sample {sample_id} is missing from the store")
    if (
        observation.machine != machine
        or observation.provider != provider
        or observation.account != account
    ):
        raise DeliveryQuotaError(
            f"pending quota sample {sample_id} does not match the bound identity"
        )
    payload = _encode_batch([observation.to_dict()])
    if len(payload) > max_bytes:
        raise DeliveryQuotaError(
            f"quota observation {sample_id} is {len(payload)} bytes, limit is {max_bytes}; "
            "sample retained"
        )
    return QuotaBatch(payload=payload, sample_id=sample_id)


def _encode_batch(observations: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {
            "protocol": PROTOCOL_VERSION,
            "kind": KIND_QUOTA_SAMPLE,
            "observations": observations,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _mark_attempt(quota_db: str | Path, sample_id: str, error: str) -> None:
    conn = _open_outbox(quota_db)
    try:
        conn.execute(
            f"UPDATE {OUTBOX_TABLE} SET attempts = attempts + 1, "
            "last_attempt_at = ?, last_error = ? WHERE sample_id = ?",
            (_utc_now(), " ".join(error.split())[:300], sample_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_delivered(quota_db: str | Path, sample_id: str) -> int:
    conn = _open_outbox(quota_db)
    try:
        changed = conn.execute(
            f"UPDATE {OUTBOX_TABLE} SET delivered_at = ?, last_error = NULL "
            "WHERE delivered_at IS NULL AND sample_id = ?",
            (_utc_now(), sample_id),
        ).rowcount
        conn.commit()
        return int(changed)
    finally:
        conn.close()


def replay_pending(
    quota_db: str | Path,
    transport: Any,
    *,
    binding: Binding,
    max_bytes: int = DEFAULT_QUOTA_MAX_BYTES,
) -> dict[str, Any]:
    """Send one pending observation at a time; mark it delivered after an ack."""

    synced = sync_outbox(quota_db, binding)
    batches = 0
    sent = 0
    delivered = 0
    duplicates = 0
    while True:
        batch = build_next_batch(quota_db, binding, max_bytes=max_bytes)
        if batch is None:
            break
        try:
            ack = transport.send(batch.payload)
        except BaseException as exc:
            _mark_attempt(quota_db, batch.sample_id, str(exc))
            raise
        try:
            ack = validate_ack(
                ack,
                accepted=1,
                kind=binding.kind,
                machine=binding.machine,
                account=binding.account,
            )
            if ack.get("provider") != binding.provider:
                raise DeliveryQuotaError(
                    "receiver acknowledgement provider does not match the binding"
                )
        except (ReplayError, DeliveryQuotaError) as exc:
            error = str(exc)
            _mark_attempt(quota_db, batch.sample_id, error)
            raise DeliveryQuotaError(error) from exc
        batches += 1
        sent += 1
        duplicates += int(ack["duplicates"])
        delivered += _mark_delivered(quota_db, batch.sample_id)
    return {
        "ok": True,
        "batches": batches,
        "synced": synced,
        "sent_observations": sent,
        "delivered": delivered,
        "duplicates": duplicates,
    }


# ---------------------------------------------------------------------------
# Trust-boundary validation


def _safe_text(value: Any, field: str, *, required: bool = False, limit: int = 200) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DeliveryQuotaError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > limit or not _SAFE_TEXT.match(text):
        raise DeliveryQuotaError(f"{field} contains control characters or is too long")
    return text


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeliveryQuotaError(f"{field} must be a finite number or null")
    number = float(value)
    if not math.isfinite(number):
        raise DeliveryQuotaError(f"{field} must be a finite number or null")
    return number


def _scalar(value: Any, field: str) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple)):
        raise DeliveryQuotaError(f"{field} must be a JSON scalar or null")
    if isinstance(value, str):
        if len(value) > 120 or not _SAFE_TEXT.match(value):
            raise DeliveryQuotaError(f"{field} contains control characters or is too long")
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise DeliveryQuotaError(f"{field} must be finite")
        return value
    raise DeliveryQuotaError(f"{field} must be a JSON scalar or null")


def _derived_display(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    if unit == "fraction":
        return max(0.0, value * 100.0)
    if unit == "percent_used":
        return max(0.0, 100.0 - value)
    raise DeliveryQuotaError("remaining source unit is required and supported")


def _normalize_window(raw: Any, parsed: Any) -> Any:
    if not isinstance(raw, Mapping):
        raise DeliveryQuotaError("each quota window must be an object")
    group = _safe_text(raw.get("group"), "window.group", required=True, limit=120)
    bucket_id = _safe_text(raw.get("bucket_id"), "window.bucket_id", required=True, limit=120)
    limit_id = _safe_text(raw.get("limit_id"), "window.limit_id", limit=120)
    slot = _safe_text(raw.get("slot"), "window.slot", limit=120)
    window = _scalar(raw.get("window"), "window.window")
    minutes = raw.get("window_minutes")
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
            raise DeliveryQuotaError("window.window_minutes must be a non-negative integer")
    remaining = raw.get("remaining")
    if remaining is None:
        remaining = {}
    if not isinstance(remaining, Mapping):
        raise DeliveryQuotaError("window.remaining must be an object")
    value = _optional_number(remaining.get("source_value"), "window.remaining.source_value")
    unit = remaining.get("source_unit")
    if unit is not None:
        if not isinstance(unit, str) or unit not in _ALLOWED_UNITS:
            raise DeliveryQuotaError("window.remaining.source_unit is not supported")
    if value is None and unit is not None:
        raise DeliveryQuotaError("window.remaining.source_unit requires a source value")
    supplied_display = _optional_number(
        remaining.get("display_percent"), "window.remaining.display_percent"
    )
    derived_display = _derived_display(value, unit)
    if supplied_display is not None and (
        derived_display is None or abs(supplied_display - derived_display) > 1e-9
    ):
        raise DeliveryQuotaError("window.remaining.display_percent is inconsistent")
    unknown = remaining.get("unknown")
    if unknown is not None:
        if not isinstance(unknown, bool) or unknown != (derived_display is None):
            raise DeliveryQuotaError("window.remaining.unknown is inconsistent")

    reset = raw.get("reset")
    if reset is None:
        reset = {}
    if not isinstance(reset, Mapping):
        raise DeliveryQuotaError("window.reset must be an object")
    reset_raw = _scalar(reset.get("raw"), "window.reset.raw")
    reset_at = reset.get("at")
    if reset_at is not None:
        reset_at = _safe_text(reset_at, "window.reset.at", required=True, limit=64)
        try:
            datetime.fromisoformat(str(reset_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise DeliveryQuotaError("window.reset.at is not RFC3339") from exc
    reset_unknown = reset.get("unknown")
    if reset_unknown is not None:
        if not isinstance(reset_unknown, bool) or reset_unknown != (reset_at is None):
            raise DeliveryQuotaError("window.reset.unknown is inconsistent")

    raw_anomalies = raw.get("anomalies")
    if raw_anomalies is None:
        raw_anomalies = []
    if not isinstance(raw_anomalies, (list, tuple)) or len(raw_anomalies) > 10:
        raise DeliveryQuotaError("window.anomalies must be a bounded list")
    anomalies: set[str] = set()
    for anomaly in raw_anomalies:
        if not isinstance(anomaly, str) or anomaly not in _ALLOWED_ANOMALIES:
            raise DeliveryQuotaError("window.anomalies contains an unsupported category")
        anomalies.add(anomaly)

    if value is None:
        anomalies.add("missing_remaining")
    elif unit == "fraction" and not 0.0 <= value <= 1.0:
        anomalies.add("remaining_out_of_range")
    elif unit == "percent_used" and not 0.0 <= value <= 100.0:
        anomalies.add("remaining_out_of_range")
    if "missing_remaining" in anomalies and value is not None:
        raise DeliveryQuotaError("window.anomalies claims a missing value that is present")
    if "invalid_reset" in anomalies and reset_at is not None:
        raise DeliveryQuotaError("window.anomalies claims an invalid reset that parsed")
    if "invalid_window" in anomalies and minutes is not None:
        raise DeliveryQuotaError("window.anomalies claims an invalid window that parsed")

    return replace(
        parsed,
        group=str(group),
        bucket_id=str(bucket_id),
        limit_id=limit_id,
        slot=slot,
        window=window,
        window_minutes=minutes,
        remaining_value=value,
        remaining_unit=unit,
        remaining_display=derived_display,
        reset_raw=reset_raw,
        reset_at=reset_at,
        anomalies=tuple(sorted(anomalies)),
    )


def _normalize_observation(raw: Any, binding: Binding) -> Any:
    quota_module = _load_quota()
    if not isinstance(raw, Mapping):
        raise DeliveryQuotaError("quota observation must be an object")
    if raw.get("schema") != "telemetre.quota.observation.v1":
        raise DeliveryQuotaError("unsupported quota observation schema")
    sample_id = raw.get("sample_id")
    if not isinstance(sample_id, str) or not _SAFE_ID.match(sample_id):
        raise DeliveryQuotaError("quota sample_id is not safe")
    for field in ("provider", "account", "machine", "sampled_at"):
        if not isinstance(raw.get(field), str) or not raw.get(field):
            raise DeliveryQuotaError(f"quota {field} must be a non-empty string")
    status = raw.get("status")
    if status not in ("success", "failure"):
        raise DeliveryQuotaError("quota observation status must be success or failure")
    interval = raw.get("interval_seconds")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise DeliveryQuotaError("quota interval_seconds must be a positive integer")
    empty_windows = raw.get("empty_windows", False)
    if not isinstance(empty_windows, bool):
        raise DeliveryQuotaError("quota empty_windows must be a boolean")
    version = raw.get("collector_version")
    if version is not None and not isinstance(version, str):
        raise DeliveryQuotaError("quota collector_version must be a string or null")
    if version is not None:
        _safe_text(version, "quota collector_version", limit=100)

    raw_windows = raw.get("windows")
    if raw_windows is None:
        raw_windows = []
    if not isinstance(raw_windows, list) or len(raw_windows) > 100:
        raise DeliveryQuotaError("quota windows must be a bounded list")
    if status == "failure" and raw_windows:
        raise DeliveryQuotaError("failure observations must not carry windows")

    error = raw.get("error")
    if status == "success":
        if error not in (None, {}):
            raise DeliveryQuotaError("success observation must not carry an error")
        error_kind = None
        error_detail = None
    else:
        if not isinstance(error, Mapping):
            raise DeliveryQuotaError("failure observation must carry an error object")
        error_kind = error.get("kind")
        if not isinstance(error_kind, str) or error_kind not in _ALLOWED_ERROR_KINDS:
            raise DeliveryQuotaError("quota error kind is not a supported category")
        if error.get("detail") is not None and not isinstance(error.get("detail"), str):
            raise DeliveryQuotaError("quota error detail must be a string or null")
        error_detail = _SAFE_ERROR_DETAIL[error_kind]

    try:
        observation = quota_module.Observation.from_dict(raw)
    except Exception as exc:
        raise DeliveryQuotaError(f"quota observation is invalid: {exc}") from exc
    if observation.machine != binding.machine:
        raise DeliveryQuotaError("quota observation machine does not match the binding")
    if observation.account != binding.account:
        raise DeliveryQuotaError("quota observation account does not match the binding")
    if observation.provider != binding.provider:
        raise DeliveryQuotaError("quota observation provider does not match the binding")
    if len(observation.windows) != len(raw_windows):
        raise DeliveryQuotaError("quota observation windows could not be parsed")

    windows = tuple(
        _normalize_window(raw_window, parsed_window)
        for raw_window, parsed_window in zip(raw_windows, observation.windows)
    )
    return replace(
        observation,
        windows=windows,
        error_kind=error_kind,
        error_detail=error_detail,
        empty_windows=(status == "success" and not windows),
    )


def parse_batch(raw: bytes, binding: Binding) -> list[Any]:
    """Validate a one-observation quota batch and enforce binding identity."""

    if binding.kind != KIND_QUOTA_SAMPLE:
        raise DeliveryQuotaError("binding is not a quota_sample binding")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeliveryQuotaError(f"invalid quota batch JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DeliveryQuotaError("quota batch must be an object")
    if payload.get("protocol") != PROTOCOL_VERSION:
        raise DeliveryQuotaError("unsupported quota protocol version")
    if payload.get("kind") != KIND_QUOTA_SAMPLE:
        raise DeliveryQuotaError("quota batch kind does not match the binding")
    observations = payload.get("observations")
    if not isinstance(observations, list) or len(observations) != 1:
        raise DeliveryQuotaError("quota batch must contain exactly one observation")
    return [_normalize_observation(observations[0], binding)]


def ingest_batch(center_db: str | Path, observations: Iterable[Any]) -> dict[str, int]:
    quota_module = _load_quota()
    items = list(observations)
    if len(items) != 1:
        raise DeliveryQuotaError("quota ingest requires exactly one observation")
    try:
        new = quota_module.ingest_observation(center_db, items[0])
    except Exception as exc:
        raise DeliveryQuotaError(f"quota store rejected a sample: {exc}") from exc
    return {"stored": int(new), "duplicates": 0 if new else 1}


__all__ = [
    "DEFAULT_QUOTA_MAX_BYTES",
    "DeliveryQuotaError",
    "OUTBOX_TABLE",
    "QuotaBatch",
    "build_next_batch",
    "ingest_batch",
    "parse_batch",
    "replay_pending",
    "sync_outbox",
]
