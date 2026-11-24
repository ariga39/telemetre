"""Standalone subscription-quota sampler for agy and Codex.

Public entry point::

    python3 -m telemetre.quota sample|report|daily ...

The slice owns a local SQLite store and exposes a small, sanitized
observation interface for the central receiver added in slice 3.  Provider
outputs never enter the database; only the fields documented in
``docs/quota.md`` are retained.

Python standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date as date_type
from datetime import datetime, time as time_type, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
OBSERVATION_SCHEMA = "telemetre.quota.observation.v1"
REPORT_SCHEMA = "telemetre.quota.report.v1"
COLLECTOR_VERSION = "telemetre-quota/0.1"

DEFAULT_CONFIG = "~/.config/telemetre/quota.json"
DEFAULT_DATABASE = "~/.local/state/telemetre/quota.sqlite3"
DEFAULT_INTERVAL_SECONDS = 15 * 60
DEFAULT_TIMEOUT_SECONDS = 60.0
STALE_INTERVALS = 2
CLOCK_SKEW_TOLERANCE_SECONDS = 5 * 60

PROVIDER_AGY = "agy"
PROVIDER_CODEX = "codex"
SUPPORTED_PROVIDERS = (PROVIDER_AGY, PROVIDER_CODEX)


class QuotaError(Exception):
    """Expected failure that the CLI can report without a traceback."""


class CollectionError(QuotaError):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class ObservationConflict(QuotaError):
    """A replayed sample id carries different content."""


# ---------------------------------------------------------------------------
# Time and JSON helpers


def parse_timestamp(value: Any) -> datetime:
    """Parse an RFC3339 timestamp.  Naive input is interpreted as UTC."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp_from_any(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return parse_timestamp(value)


def format_timestamp(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    parsed = _timestamp_from_any(value)
    text = parsed.isoformat(timespec="microseconds")
    text = text.replace("+00:00", "Z")
    if text.endswith(".000000Z"):
        text = text[:-8] + "Z"
    return text


def _epoch(value: datetime | str | None) -> float | None:
    if value is None:
        return None
    return _timestamp_from_any(value).timestamp()


def _now(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return _timestamp_from_any(value)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _json_loads_strict(text: str):
    """Parse provider JSON without NaN/Infinity, including overflow floats."""

    return json.loads(
        text,
        parse_constant=_reject_json_constant,
        parse_float=_finite_json_float,
    )


def _json_load(text: str | None, default=None):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _duration_display(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d ({minutes}m)"
    if minutes % 60 == 0:
        return f"{minutes // 60}h ({minutes}m)"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Sanitized observation model (public seam for slice 3)


@dataclass(frozen=True)
class WindowObservation:
    group: str
    bucket_id: str
    limit_id: str | None = None
    slot: str | None = None
    window: Any = None
    window_minutes: int | None = None
    remaining_value: float | None = None
    remaining_unit: str | None = None
    remaining_display: float | None = None
    reset_raw: Any = None
    reset_at: str | None = None
    anomalies: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "bucket_id": self.bucket_id,
            "limit_id": self.limit_id,
            "slot": self.slot,
            "window": self.window,
            "window_minutes": self.window_minutes,
            "window_display": _duration_display(self.window_minutes),
            "remaining": {
                "source_value": self.remaining_value,
                "source_unit": self.remaining_unit,
                "display_percent": self.remaining_display,
                "unknown": self.remaining_display is None,
            },
            "reset": {
                "raw": self.reset_raw,
                "at": self.reset_at,
                "unknown": self.reset_at is None,
            },
            "anomalies": list(self.anomalies),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WindowObservation":
        remaining = data.get("remaining") or {}
        reset = data.get("reset") or {}
        return cls(
            group=str(data.get("group") or ""),
            bucket_id=str(data.get("bucket_id") or ""),
            limit_id=data.get("limit_id"),
            slot=data.get("slot"),
            window=data.get("window"),
            window_minutes=data.get("window_minutes"),
            remaining_value=remaining.get("source_value"),
            remaining_unit=remaining.get("source_unit"),
            remaining_display=remaining.get("display_percent"),
            reset_raw=reset.get("raw"),
            reset_at=reset.get("at"),
            anomalies=tuple(data.get("anomalies") or ()),
        )


@dataclass(frozen=True)
class Observation:
    provider: str
    account: str
    machine: str
    sampled_at: str
    status: str
    windows: tuple[WindowObservation, ...] = ()
    error_kind: str | None = None
    error_detail: str | None = None
    empty_windows: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    sample_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema: str = OBSERVATION_SCHEMA
    collector_version: str = COLLECTOR_VERSION

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "sample_id": self.sample_id,
            "collector_version": self.collector_version,
            "machine": self.machine,
            "provider": self.provider,
            "account": self.account,
            "sampled_at": self.sampled_at,
            "interval_seconds": self.interval_seconds,
            "status": self.status,
            "error": (
                None
                if self.status == "success"
                else {"kind": self.error_kind, "detail": self.error_detail}
            ),
            "empty_windows": self.empty_windows,
            "windows": [window.to_dict() for window in self.windows],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Observation":
        if not isinstance(data, Mapping):
            raise ValueError("observation must be an object")
        if data.get("schema") != OBSERVATION_SCHEMA:
            raise ValueError("unsupported observation schema")
        provider = str(data.get("provider") or "")
        account = str(data.get("account") or "")
        machine = str(data.get("machine") or "")
        sampled_at = format_timestamp(data.get("sampled_at"))
        status = str(data.get("status") or "")
        sample_id = str(data.get("sample_id") or "")
        if not provider or not account or not machine or not sampled_at or not sample_id:
            raise ValueError("observation is missing required identity fields")
        if status not in ("success", "failure"):
            raise ValueError("observation status must be success or failure")
        windows = tuple(WindowObservation.from_dict(item) for item in data.get("windows") or ())
        error = data.get("error") or {}
        interval = data.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            raise ValueError("interval_seconds must be an integer") from None
        if interval <= 0:
            raise ValueError("interval_seconds must be positive")
        observation = cls(
            provider=provider,
            account=account,
            machine=machine,
            sampled_at=sampled_at,
            status=status,
            windows=windows,
            error_kind=(error.get("kind") if isinstance(error, Mapping) else None),
            error_detail=(error.get("detail") if isinstance(error, Mapping) else None),
            empty_windows=bool(data.get("empty_windows")),
            interval_seconds=interval,
            sample_id=sample_id,
            schema=OBSERVATION_SCHEMA,
            collector_version=str(data.get("collector_version") or ""),
        )
        _validate_observation(observation)
        return observation


@dataclass(frozen=True)
class SourceConfig:
    provider: str
    account: str
    command: tuple[str, ...]
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Config:
    machine: str
    database: str | None
    timezone: str
    sources: tuple[SourceConfig, ...]


def load_config(path: str | os.PathLike | None) -> Config:
    if path is None:
        path = os.environ.get("TELEMETRE_QUOTA_CONFIG", DEFAULT_CONFIG)
    config_path = Path(path).expanduser()
    try:
        payload = _json_loads_strict(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QuotaError(f"config file not found: {config_path}") from None
    except (OSError, ValueError) as exc:
        raise QuotaError(f"cannot read config {config_path}: {exc}") from None
    if not isinstance(payload, dict):
        raise QuotaError("config must be a JSON object")
    machine = str(payload.get("machine") or "unknown")
    database = payload.get("database")
    tz_name = str(payload.get("timezone") or "UTC")
    _zone(tz_name)
    raw_sources = payload.get("providers")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise QuotaError("config.providers must be a non-empty list")
    sources: list[SourceConfig] = []
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise QuotaError("each config.providers entry must be an object")
        provider = str(raw.get("provider") or "").strip().lower()
        account = str(raw.get("account") or "").strip()
        if provider not in SUPPORTED_PROVIDERS:
            raise QuotaError(f"unsupported provider: {provider or '<missing>'}")
        if not account:
            raise QuotaError("provider account must not be empty")
        try:
            interval = int(raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            raise QuotaError("interval_seconds must be an integer") from None
        if interval <= 0:
            raise QuotaError("interval_seconds must be positive")
        try:
            timeout = float(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            raise QuotaError("timeout_seconds must be a number") from None
        if timeout <= 0 or not math.isfinite(timeout):
            raise QuotaError("timeout_seconds must be positive and finite")
        command = raw.get("command")
        if command is None:
            command = _default_command(provider)
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise QuotaError("command must be a non-empty list of strings")
        sources.append(
            SourceConfig(
                provider=provider,
                account=account,
                command=tuple(command),
                interval_seconds=interval,
                timeout_seconds=timeout,
            )
        )
    return Config(
        machine=machine,
        database=str(database) if database else None,
        timezone=tz_name,
        sources=tuple(sources),
    )


def _default_command(provider: str) -> list[str]:
    if provider == PROVIDER_AGY:
        return ["agy", "-p", "/usage", "--output-format", "json"]
    return ["codex", "app-server"]


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise QuotaError(f"unknown timezone: {name}") from None


# ---------------------------------------------------------------------------
# Provider subprocesses


_AUTH_PATTERN = re.compile(
    r"(?:log[\s-]?in|sign[\s-]?in|reauth|re-auth|unauthori[sz]ed|authentication|"
    r"not\s+logged|session\s+expired|\b401\b)",
    re.IGNORECASE,
)


def _error_detail_from_text(text: str | None) -> str:
    if text and _AUTH_PATTERN.search(text):
        return "provider requires authentication"
    return "provider returned an error"


def _process_group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Terminate the child and any descendants it left in its process group.

    The leader may already have exited while a descendant keeps the group
    alive, so group membership is checked even after ``poll()`` returns.
    """

    group = process.pid
    if _process_group_alive(group):
        try:
            os.killpg(group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                process.terminate()
            except ProcessLookupError:
                pass
    if process.poll() is None:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + 1.0
    while _process_group_alive(group) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _process_group_alive(group):
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _run_capture(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError:
        raise CollectionError("spawn_error", "provider command not found") from None
    except OSError as exc:
        raise CollectionError("spawn_error", f"cannot start provider command: {exc.errno}") from None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        try:
            process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        raise CollectionError("timeout", f"provider did not finish within {timeout:g}s") from None
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _collect_agy(source: SourceConfig) -> tuple[tuple[WindowObservation, ...], bool]:
    completed = _run_capture(source.command, source.timeout_seconds)
    if completed.returncode != 0:
        raise CollectionError(
            "not_authenticated" if _AUTH_PATTERN.search(completed.stderr or "") else "exit_code",
            _error_detail_from_text(completed.stderr),
        )
    try:
        payload = _json_loads_strict(completed.stdout)
    except (TypeError, ValueError):
        raise CollectionError("invalid_json", "provider output was not valid finite JSON") from None
    if not isinstance(payload, dict):
        raise CollectionError("unrecognized_structure", "provider JSON root is not an object")
    status = payload.get("status")
    if not isinstance(status, str) or status.lower() not in ("success", "ok"):
        text = json.dumps(payload, ensure_ascii=False)[:4000]
        raise CollectionError(
            "not_authenticated" if _AUTH_PATTERN.search(text) else (
                "provider_error" if status is not None else "unrecognized_structure"
            ),
            _error_detail_from_text(text) if status is not None else "missing success status",
        )
    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise CollectionError("unrecognized_structure", "missing command.data.groups")

    windows: list[WindowObservation] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
            raise CollectionError("unrecognized_structure", "malformed quota group")
        group_name = group.get("name")
        if not isinstance(group_name, str) or not group_name:
            raise CollectionError("unrecognized_structure", "quota group has no name")
        for bucket in group["buckets"]:
            if not isinstance(bucket, dict):
                raise CollectionError("unrecognized_structure", "malformed quota bucket")
            bucket_id = bucket.get("id")
            if not isinstance(bucket_id, (str, int)) or str(bucket_id) == "":
                raise CollectionError("unrecognized_structure", "quota bucket has no id")
            amount = bucket.get("remaining_fraction")
            anomalies: list[str] = []
            display = None
            unit = None
            numeric_amount = None
            if amount is None:
                anomalies.append("missing_remaining")
            elif isinstance(amount, bool) or not isinstance(amount, (int, float)):
                raise CollectionError("unrecognized_structure", "remaining_fraction is not numeric")
            else:
                numeric_amount = float(amount)
                if not math.isfinite(numeric_amount):
                    raise CollectionError(
                        "unrecognized_structure", "remaining_fraction is not finite"
                    )
                unit = "fraction"
                display = max(0.0, numeric_amount * 100.0)
                if not 0.0 <= numeric_amount <= 1.0:
                    anomalies.append("remaining_out_of_range")
            reset_raw = bucket.get("reset_time")
            reset_at, reset_anomaly = _parse_reset_value(reset_raw)
            if reset_anomaly:
                anomalies.append(reset_anomaly)
            windows.append(
                WindowObservation(
                    group=group_name,
                    bucket_id=str(bucket_id),
                    window=bucket.get("window"),
                    window_minutes=_window_minutes(bucket.get("window")),
                    remaining_value=numeric_amount,
                    remaining_unit=unit,
                    remaining_display=display,
                    reset_raw=reset_raw,
                    reset_at=reset_at,
                    anomalies=tuple(anomalies),
                )
            )
    return tuple(windows), not windows


def _parse_reset_value(raw: Any) -> tuple[str | None, str | None]:
    if raw is None or raw == 0 or raw == "0" or raw == "":
        return None, None
    try:
        return format_timestamp(parse_timestamp(raw)), None
    except (TypeError, ValueError):
        return None, "invalid_reset"


def _window_minutes(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float) and raw.is_integer() and raw >= 0:
        return int(raw)
    if isinstance(raw, str):
        match = re.fullmatch(r"\s*(\d+)\s*(m|min|mins|minute|minutes|h|hr|hour|hours|d|day|days)\s*", raw, re.I)
        if not match:
            return None
        value = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("d"):
            return value * 1440
        if unit.startswith("h"):
            return value * 60
        return value
    return None


class _ProtocolTimeout(Exception):
    pass


class _ProtocolEOF(Exception):
    pass


class _LineReader:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self._buffer = b""
        self._selector = selectors.DefaultSelector()
        self._selector.register(process.stdout, selectors.EVENT_READ)

    def close(self) -> None:
        self._selector.close()

    def read_message(self, deadline: float) -> dict:
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ProtocolTimeout()
            events = self._selector.select(remaining)
            if not events:
                raise _ProtocolTimeout()
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise _ProtocolEOF()
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return self.read_message(deadline)
        try:
            message = _json_loads_strict(text)
        except ValueError:
            raise CollectionError(
                "protocol_error", "provider sent an invalid protocol line"
            ) from None
        if not isinstance(message, dict):
            raise CollectionError("protocol_error", "provider sent a non-object protocol message")
        return message


def _send_message(process: subprocess.Popen, message: Mapping[str, Any]) -> None:
    if process.stdin is None:
        raise CollectionError("protocol_error", "provider stdin is unavailable")
    try:
        process.stdin.write((_json_dump(message) + "\n").encode("utf-8"))
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        raise CollectionError("protocol_error", "provider closed its input") from None


def _await_response(reader: _LineReader, request_id: int, deadline: float) -> dict:
    while True:
        try:
            message = reader.read_message(deadline)
        except _ProtocolTimeout:
            raise CollectionError("timeout", "provider protocol response timed out") from None
        except _ProtocolEOF:
            raise CollectionError("protocol_error", "provider closed its output") from None
        if message.get("id") != request_id:
            continue
        error = message.get("error")
        if error is not None:
            text = _json_dump(error)
            auth = bool(_AUTH_PATTERN.search(text))
            raise CollectionError(
                "not_authenticated" if auth else "provider_error",
                _error_detail_from_text(text),
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise CollectionError("protocol_error", "provider response has no result object")
        return result


def _collect_codex(source: SourceConfig) -> tuple[tuple[WindowObservation, ...], bool]:
    try:
        process = subprocess.Popen(
            list(source.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=0,
        )
    except FileNotFoundError:
        raise CollectionError("spawn_error", "provider command not found") from None
    except OSError as exc:
        raise CollectionError("spawn_error", f"cannot start provider command: {exc.errno}") from None
    deadline = time.monotonic() + source.timeout_seconds
    reader = _LineReader(process)
    try:
        _send_message(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "telemetre", "version": COLLECTOR_VERSION}},
            },
        )
        _await_response(reader, 1, deadline)
        _send_message(process, {"method": "initialized", "params": {}})
        _send_message(process, {"id": 2, "method": "account/rateLimits/read", "params": {}})
        result = _await_response(reader, 2, deadline)
    finally:
        reader.close()
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        _terminate_process_group(process)
    return _windows_from_codex_result(result)


def _windows_from_codex_result(result: Mapping[str, Any]) -> tuple[tuple[WindowObservation, ...], bool]:
    by_limit = result.get("rateLimitsByLimitId")
    single = result.get("rateLimits")
    if isinstance(by_limit, dict) and by_limit:
        mapping: Mapping[str, Any] = by_limit
        use_mapping = True
    elif isinstance(single, dict):
        mapping = {"__single__": single}
        use_mapping = False
    elif isinstance(by_limit, dict):
        return (), True
    else:
        raise CollectionError("unrecognized_structure", "missing Codex rate limit views")

    windows: list[WindowObservation] = []
    for map_key, limit in mapping.items():
        if not isinstance(limit, dict):
            raise CollectionError("unrecognized_structure", "malformed Codex rate limit")
        limit_id = limit.get("limitId")
        if limit_id is None and use_mapping:
            limit_id = map_key
        if limit_id is not None and not isinstance(limit_id, str):
            raise CollectionError("unrecognized_structure", "Codex limitId is not a string")
        for slot in ("primary", "secondary"):
            raw_slot = limit.get(slot)
            if raw_slot is None:
                continue
            if not isinstance(raw_slot, dict):
                raise CollectionError("unrecognized_structure", "malformed Codex quota slot")
            windows.append(_window_from_codex_slot(limit_id, slot, raw_slot))
    return tuple(windows), not windows


def _window_from_codex_slot(
    limit_id: str | None, slot: str, raw_slot: Mapping[str, Any]
) -> WindowObservation:
    anomalies: list[str] = []
    used = raw_slot.get("usedPercent")
    numeric_used = None
    display = None
    unit = None
    if used is None:
        anomalies.append("missing_remaining")
    elif isinstance(used, bool) or not isinstance(used, (int, float)):
        raise CollectionError("unrecognized_structure", "usedPercent is not numeric")
    else:
        numeric_used = float(used)
        if not math.isfinite(numeric_used):
            raise CollectionError(
                "unrecognized_structure", "usedPercent is not finite"
            )
        unit = "percent_used"
        display = max(0.0, 100.0 - numeric_used)
        if not 0.0 <= numeric_used <= 100.0:
            anomalies.append("remaining_out_of_range")
    duration = raw_slot.get("windowDurationMins")
    minutes = None
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise CollectionError("unrecognized_structure", "windowDurationMins is not numeric")
        if not math.isfinite(float(duration)):
            raise CollectionError(
                "unrecognized_structure", "windowDurationMins is not finite"
            )
        minutes = int(duration)
        if minutes < 0:
            anomalies.append("invalid_window")
    resets_raw = raw_slot.get("resetsAt")
    reset_at = None
    if resets_raw is None or resets_raw == 0 or resets_raw == "0":
        pass
    elif (
        isinstance(resets_raw, bool)
        or not isinstance(resets_raw, (int, float))
        or not math.isfinite(float(resets_raw))
    ):
        raise CollectionError("unrecognized_structure", "resetsAt is not finite")
    else:
        try:
            reset_at = format_timestamp(
                datetime.fromtimestamp(float(resets_raw), tz=UTC)
            )
        except (OverflowError, OSError, ValueError):
            anomalies.append("invalid_reset")
    return WindowObservation(
        group=limit_id or "default",
        bucket_id=slot,
        limit_id=limit_id,
        slot=slot,
        window=minutes,
        window_minutes=minutes,
        remaining_value=numeric_used,
        remaining_unit=unit,
        remaining_display=display,
        reset_raw=resets_raw,
        reset_at=reset_at,
        anomalies=tuple(anomalies),
    )


def collect_observation(
    source: SourceConfig,
    *,
    machine: str,
    sampled_at: Any = None,
    sample_id: str | None = None,
) -> Observation:
    timestamp = format_timestamp(_now(sampled_at))
    try:
        if source.provider == PROVIDER_AGY:
            windows, empty = _collect_agy(source)
        elif source.provider == PROVIDER_CODEX:
            windows, empty = _collect_codex(source)
        else:
            raise CollectionError("unsupported_provider", f"unsupported provider: {source.provider}")
    except CollectionError as exc:
        return Observation(
            provider=source.provider,
            account=source.account,
            machine=machine,
            sampled_at=timestamp,
            status="failure",
            error_kind=exc.kind,
            error_detail=exc.detail,
            interval_seconds=source.interval_seconds,
            sample_id=sample_id or uuid.uuid4().hex,
        )
    except Exception:
        # A provider can return a shape we did not anticipate.  Record a
        # structural failure rather than crashing the sampler or replacing
        # the last complete snapshot.
        return Observation(
            provider=source.provider,
            account=source.account,
            machine=machine,
            sampled_at=timestamp,
            status="failure",
            error_kind="unrecognized_structure",
            error_detail="provider response could not be interpreted",
            interval_seconds=source.interval_seconds,
            sample_id=sample_id or uuid.uuid4().hex,
        )
    return Observation(
        provider=source.provider,
        account=source.account,
        machine=machine,
        sampled_at=timestamp,
        status="success",
        windows=windows,
        empty_windows=empty,
        interval_seconds=source.interval_seconds,
        sample_id=sample_id or uuid.uuid4().hex,
    )


# ---------------------------------------------------------------------------
# SQLite store


_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_samples (
    sample_id        TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,
    account          TEXT NOT NULL,
    machine          TEXT NOT NULL,
    sampled_at       TEXT NOT NULL,
    sampled_epoch    REAL NOT NULL,
    received_at      TEXT NOT NULL,
    received_epoch   REAL NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('success', 'failure')),
    error_kind       TEXT,
    error_detail     TEXT,
    empty_windows    INTEGER NOT NULL DEFAULT 0,
    time_anomaly     INTEGER NOT NULL DEFAULT 0,
    interval_seconds INTEGER NOT NULL,
    collector_version TEXT NOT NULL,
    payload_digest   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quota_samples_account_time
    ON quota_samples (provider, account, sampled_epoch DESC, received_epoch DESC);
CREATE INDEX IF NOT EXISTS quota_samples_time
    ON quota_samples (sampled_epoch);

CREATE TABLE IF NOT EXISTS quota_windows (
    sample_id         TEXT NOT NULL REFERENCES quota_samples(sample_id) ON DELETE CASCADE,
    ordinal           INTEGER NOT NULL,
    group_name        TEXT NOT NULL,
    bucket_id         TEXT NOT NULL,
    limit_id          TEXT,
    slot              TEXT,
    window_raw        TEXT,
    window_minutes    INTEGER,
    remaining_value   REAL,
    remaining_unit    TEXT,
    remaining_display REAL,
    reset_raw         TEXT,
    reset_at          TEXT,
    anomalies         TEXT NOT NULL,
    PRIMARY KEY (sample_id, ordinal)
);

CREATE TABLE IF NOT EXISTS quota_daily (
    day          TEXT NOT NULL,
    timezone     TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    payload      TEXT NOT NULL,
    PRIMARY KEY (day, timezone)
);
"""


def connect_database(path: str | os.PathLike) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise QuotaError(f"cannot create database directory {db_path.parent}: {exc}") from None
    try:
        connection = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error as exc:
        raise QuotaError(f"cannot open database {db_path}: {exc}") from None
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(_SCHEMA)
    connection.commit()
    return connection


def _validate_observation(observation: Observation) -> None:
    if observation.provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider: {observation.provider}")
    if not observation.account or not observation.machine:
        raise ValueError("account and machine must not be empty")
    if observation.status not in ("success", "failure"):
        raise ValueError("invalid status")
    if observation.status == "failure" and observation.windows:
        raise ValueError("failure observations must not carry windows")
    if not observation.sample_id:
        raise ValueError("sample_id must not be empty")
    if observation.interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    _timestamp_from_any(observation.sampled_at)
    for window in observation.windows:
        if not window.group or not window.bucket_id:
            raise ValueError("quota window has an empty group or bucket id")
        for name, value in (
            ("remaining_value", window.remaining_value),
            ("remaining_display", window.remaining_display),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if window.window_minutes is not None:
            if isinstance(window.window_minutes, bool) or not isinstance(
                window.window_minutes, int
            ):
                raise ValueError("window_minutes must be an integer")
            if window.window_minutes < 0:
                raise ValueError("window_minutes must not be negative")
        if window.reset_at is not None:
            _timestamp_from_any(window.reset_at)


def _observation_row_to_model(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> Observation:
    windows = []
    for window_row in _windows_for_sample(connection, row["sample_id"]):
        windows.append(
            WindowObservation(
                group=window_row["group_name"],
                bucket_id=window_row["bucket_id"],
                limit_id=window_row["limit_id"],
                slot=window_row["slot"],
                window=_json_load(window_row["window_raw"]),
                window_minutes=window_row["window_minutes"],
                remaining_value=window_row["remaining_value"],
                remaining_unit=window_row["remaining_unit"],
                remaining_display=window_row["remaining_display"],
                reset_raw=_json_load(window_row["reset_raw"]),
                reset_at=window_row["reset_at"],
                anomalies=tuple(_json_load(window_row["anomalies"], [])),
            )
        )
    return Observation(
        provider=row["provider"],
        account=row["account"],
        machine=row["machine"],
        sampled_at=row["sampled_at"],
        status=row["status"],
        windows=tuple(windows),
        error_kind=row["error_kind"],
        error_detail=row["error_detail"],
        empty_windows=bool(row["empty_windows"]),
        interval_seconds=int(row["interval_seconds"]),
        sample_id=row["sample_id"],
        collector_version=row["collector_version"],
    )


def iter_observations(
    database: str | os.PathLike,
    *,
    provider: str | None = None,
    account: str | None = None,
    machine: str | None = None,
    sampled_after: Any = None,
) -> Iterable[Observation]:
    """Yield stored observations in sample-time order.

    This is the read side of the slice-3 delivery seam.  It returns the same
    sanitized model accepted by :func:`ingest_observation`, including failure
    observations.  Acknowledgement bookkeeping belongs to the delivery layer;
    this module never deletes a sample.
    """

    clauses = []
    parameters: list[Any] = []
    if provider is not None:
        clauses.append("provider = ?")
        parameters.append(provider)
    if account is not None:
        clauses.append("account = ?")
        parameters.append(account)
    if machine is not None:
        clauses.append("machine = ?")
        parameters.append(machine)
    if sampled_after is not None:
        clauses.append("sampled_epoch > ?")
        parameters.append(_timestamp_from_any(sampled_after).timestamp())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = connect_database(database)
    try:
        rows = connection.execute(
            f"""
            SELECT * FROM quota_samples
            {where}
            ORDER BY sampled_epoch ASC, received_epoch ASC, sample_id ASC
            """,
            parameters,
        ).fetchall()
        for row in rows:
            yield _observation_row_to_model(connection, row)
    finally:
        connection.close()


def read_observation(
    database: str | os.PathLike, sample_id: str
) -> Observation | None:
    """Read one sanitized observation by its stable sample id."""

    connection = connect_database(database)
    try:
        row = connection.execute(
            "SELECT * FROM quota_samples WHERE sample_id = ?", (sample_id,)
        ).fetchone()
        if row is None:
            return None
        return _observation_row_to_model(connection, row)
    finally:
        connection.close()


def _observation_digest(observation: Observation) -> str:
    return hashlib.sha256(_json_dump(observation.to_dict()).encode("utf-8")).hexdigest()


def ingest_observation(
    database: str | os.PathLike,
    observation: Observation | Mapping[str, Any],
    *,
    received_at: Any = None,
) -> bool:
    """Store a sanitized observation.  Returns True when it was new.

    Replaying the same sample id and content is a no-op.  Reusing an id with
    different content raises :class:`ObservationConflict`.
    """

    if not isinstance(observation, Observation):
        observation = Observation.from_dict(observation)
    _validate_observation(observation)
    received = _now(received_at)
    sampled = _timestamp_from_any(observation.sampled_at)
    time_anomaly = sampled > received + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS)
    digest = _observation_digest(observation)

    connection = connect_database(database)
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO quota_samples (
                    sample_id, provider, account, machine, sampled_at, sampled_epoch,
                    received_at, received_epoch, status, error_kind, error_detail,
                    empty_windows, time_anomaly, interval_seconds, collector_version,
                    payload_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.sample_id,
                    observation.provider,
                    observation.account,
                    observation.machine,
                    format_timestamp(sampled),
                    sampled.timestamp(),
                    format_timestamp(received),
                    received.timestamp(),
                    observation.status,
                    observation.error_kind,
                    observation.error_detail,
                    int(observation.empty_windows),
                    int(time_anomaly),
                    observation.interval_seconds,
                    observation.collector_version,
                    digest,
                ),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    "SELECT payload_digest FROM quota_samples WHERE sample_id = ?",
                    (observation.sample_id,),
                ).fetchone()
                if existing is None:
                    raise ObservationConflict("sample id disappeared during insert")
                if existing["payload_digest"] != digest:
                    raise ObservationConflict(
                        f"sample id {observation.sample_id} already exists with different content"
                    )
                return False
            for ordinal, window in enumerate(observation.windows):
                connection.execute(
                    """
                    INSERT INTO quota_windows (
                        sample_id, ordinal, group_name, bucket_id, limit_id, slot,
                        window_raw, window_minutes, remaining_value, remaining_unit,
                        remaining_display, reset_raw, reset_at, anomalies
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.sample_id,
                        ordinal,
                        window.group,
                        window.bucket_id,
                        window.limit_id,
                        window.slot,
                        _json_dump(window.window),
                        window.window_minutes,
                        window.remaining_value,
                        window.remaining_unit,
                        window.remaining_display,
                        _json_dump(window.reset_raw),
                        window.reset_at,
                        _json_dump(list(window.anomalies)),
                    ),
                )
            return True
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Reports


def _source_identity(sources: Iterable[SourceConfig]) -> dict[tuple[str, str], int]:
    return {(source.provider, source.account): source.interval_seconds for source in sources}


def _database_accounts(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = connection.execute(
        "SELECT DISTINCT provider, account FROM quota_samples ORDER BY provider, account"
    ).fetchall()
    return [(row["provider"], row["account"]) for row in rows]


def _latest_sample(connection: sqlite3.Connection, provider: str, account: str):
    return connection.execute(
        """
        SELECT * FROM quota_samples
        WHERE provider = ? AND account = ?
        ORDER BY sampled_epoch DESC, received_epoch DESC, sample_id DESC
        LIMIT 1
        """,
        (provider, account),
    ).fetchone()


def _latest_success(connection: sqlite3.Connection, provider: str, account: str):
    return connection.execute(
        """
        SELECT * FROM quota_samples
        WHERE provider = ? AND account = ? AND status = 'success'
        ORDER BY sampled_epoch DESC, received_epoch DESC, sample_id DESC
        LIMIT 1
        """,
        (provider, account),
    ).fetchone()


def _windows_for_sample(connection: sqlite3.Connection, sample_id: str):
    return connection.execute(
        "SELECT * FROM quota_windows WHERE sample_id = ? ORDER BY ordinal",
        (sample_id,),
    ).fetchall()


def _public_window(
    row: sqlite3.Row,
    sampled_at: str,
    machine: str,
    *,
    now: datetime,
    stale: bool,
    change: dict | None = None,
    time_anomaly: bool = False,
) -> dict:
    display = row["remaining_display"]
    reset_at = row["reset_at"]
    pending = bool(reset_at and _timestamp_from_any(reset_at) <= now and _timestamp_from_any(sampled_at) < _timestamp_from_any(reset_at))
    age = max(0, int((now - _timestamp_from_any(sampled_at)).total_seconds()))
    return {
        "group": row["group_name"],
        "bucket_id": row["bucket_id"],
        "limit_id": row["limit_id"],
        "slot": row["slot"],
        "window": _json_load(row["window_raw"]),
        "window_minutes": row["window_minutes"],
        "window_display": _duration_display(row["window_minutes"]),
        "remaining": {
            "source_value": row["remaining_value"],
            "source_unit": row["remaining_unit"],
            "display_percent": display,
            "unknown": display is None,
        },
        "reset": {
            "raw": _json_load(row["reset_raw"]),
            "at": reset_at,
            "unknown": reset_at is None,
        },
        "sampled_at": sampled_at,
        "machine": machine,
        "freshness": {
            "age_seconds": age,
            "stale": stale,
            "pending_refresh": pending,
        },
        "anomalies": _json_load(row["anomalies"], []),
        "time_anomaly": bool(time_anomaly),
        "change": change,
    }


def _change_for_window(
    connection: sqlite3.Connection,
    *,
    provider: str,
    account: str,
    row: sqlite3.Row,
    current_sampled_at: str,
    current_epoch: float,
    interval_seconds: int,
) -> dict | None:
    previous = connection.execute(
        """
        SELECT w.*, s.sampled_at AS sample_sampled_at, s.sampled_epoch AS sample_sampled_epoch
        FROM quota_windows w
        JOIN quota_samples s ON s.sample_id = w.sample_id
        WHERE s.provider = ? AND s.account = ? AND s.status = 'success'
          AND s.sampled_epoch < ?
          AND w.group_name = ? AND w.bucket_id = ?
          AND w.limit_id IS ? AND w.slot IS ?
        ORDER BY s.sampled_epoch DESC, s.received_epoch DESC, s.sample_id DESC
        LIMIT 1
        """,
        (
            provider,
            account,
            current_epoch,
            row["group_name"],
            row["bucket_id"],
            row["limit_id"],
            row["slot"],
        ),
    ).fetchone()
    if previous is None:
        return None
    previous_at = previous["sample_sampled_at"]
    if row["reset_at"] is None or previous["reset_at"] is None:
        return {
            "comparable": False,
            "reason": "unknown_reset",
            "previous_sampled_at": previous_at,
        }
    if row["reset_at"] != previous["reset_at"]:
        return {
            "comparable": False,
            "reason": "window_refreshed",
            "previous_sampled_at": previous_at,
        }
    if row["window_minutes"] != previous["window_minutes"]:
        return {
            "comparable": False,
            "reason": "window_changed",
            "previous_sampled_at": previous_at,
        }
    gap = current_epoch - float(previous["sample_sampled_epoch"])
    if gap > STALE_INTERVALS * interval_seconds:
        return {
            "comparable": False,
            "reason": "sampling_gap",
            "previous_sampled_at": previous_at,
        }
    if row["remaining_display"] is None or previous["remaining_display"] is None:
        return {
            "comparable": False,
            "reason": "unknown_remaining",
            "previous_sampled_at": previous_at,
        }
    return {
        "comparable": True,
        "reason": None,
        "previous_percent": previous["remaining_display"],
        "delta_percent": round(
            float(row["remaining_display"]) - float(previous["remaining_display"]), 6
        ),
        "previous_sampled_at": previous_at,
    }


def current_report(
    database: str | os.PathLike,
    *,
    config: Config | None = None,
    now: Any = None,
    tz: str | None = None,
) -> dict:
    generated = _now(now)
    timezone_name = tz or (config.timezone if config else "UTC")
    _zone(timezone_name)
    configured = _source_identity(config.sources) if config else {}
    connection = connect_database(database)
    try:
        accounts = list(configured)
        for identity in _database_accounts(connection):
            if identity not in accounts:
                accounts.append(identity)
        result_accounts = []
        for provider, account in accounts:
            latest = _latest_sample(connection, provider, account)
            success = _latest_success(connection, provider, account)
            interval = configured.get((provider, account))
            if interval is None and success is not None:
                interval = int(success["interval_seconds"])
            if interval is None and latest is not None:
                interval = int(latest["interval_seconds"])
            interval = interval or DEFAULT_INTERVAL_SECONDS
            missing = latest is None
            stale = True
            age = None
            if success is not None:
                age = max(0, int((generated - _timestamp_from_any(success["sampled_at"])).total_seconds()))
                stale = age > STALE_INTERVALS * interval
            last_attempt = None
            if latest is not None:
                last_attempt = {
                    "sample_id": latest["sample_id"],
                    "status": latest["status"],
                    "sampled_at": latest["sampled_at"],
                    "received_at": latest["received_at"],
                    "machine": latest["machine"],
                    "error_kind": latest["error_kind"],
                    "error_detail": latest["error_detail"],
                    "time_anomaly": bool(latest["time_anomaly"]),
                }
            last_success = None
            if success is not None:
                last_success = {
                    "sample_id": success["sample_id"],
                    "sampled_at": success["sampled_at"],
                    "received_at": success["received_at"],
                    "machine": success["machine"],
                    "time_anomaly": bool(success["time_anomaly"]),
                }
            windows = []
            empty_windows = False
            if success is not None:
                empty_windows = bool(success["empty_windows"])
                for row in _windows_for_sample(connection, success["sample_id"]):
                    change = _change_for_window(
                        connection,
                        provider=provider,
                        account=account,
                        row=row,
                        current_sampled_at=success["sampled_at"],
                        current_epoch=float(success["sampled_epoch"]),
                        interval_seconds=interval,
                    )
                    windows.append(
                        _public_window(
                            row,
                            success["sampled_at"],
                            success["machine"],
                            now=generated,
                            stale=stale,
                            change=change,
                            time_anomaly=bool(success["time_anomaly"]),
                        )
                    )
            result_accounts.append(
                {
                    "provider": provider,
                    "account": account,
                    "interval_seconds": interval,
                    "missing": missing,
                    "stale": stale,
                    "age_seconds": age,
                    "last_attempt": last_attempt,
                    "last_success": last_success,
                    "empty_windows": empty_windows,
                    "windows": windows,
                    "missing_gaps": [],
                }
            )
        return {
            "schema": REPORT_SCHEMA,
            "mode": "current",
            "generated_at": format_timestamp(generated),
            "timezone": timezone_name,
            "accounts": result_accounts,
        }
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Text rendering


def _percent(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.1f}%"


def render_current(report: Mapping[str, Any]) -> str:
    lines = [
        f"Quota report (current) · {report['generated_at']} · timezone {report['timezone']}"
    ]
    for account in report["accounts"]:
        latest = account["last_attempt"]
        if latest is None:
            state = "no sample yet"
        elif latest["status"] == "success":
            state = f"last attempt success at {latest['sampled_at']}"
        else:
            state = (
                f"last attempt FAILURE ({latest['error_kind']}) at {latest['sampled_at']}"
            )
        flags = []
        if account["missing"]:
            flags.append("MISSING")
        if account["stale"]:
            flags.append("STALE (>2 intervals)")
        lines.append(
            f"\n{account['provider']}/{account['account']}  {state}"
            + (f"  [{', '.join(flags)}]" if flags else "")
        )
        if account["last_success"] is not None:
            lines.append(f"  last success: {account['last_success']['sampled_at']}")
        if not account["windows"]:
            lines.append(
                "  no windows returned"
                if account["empty_windows"]
                else "  no successful window snapshot"
            )
            continue
        lines.append(
            "  group / bucket / window   remaining   reset   sampled   source"
        )
        for window in account["windows"]:
            reset = window["reset"]["at"] or "unknown"
            name = f"{window['group']} / {window['bucket_id']}"
            if window["window_display"]:
                name += f" / {window['window_display']}"
            flags = list(window["anomalies"])
            if window["freshness"]["pending_refresh"]:
                flags.append("REFRESH PENDING")
            if window["freshness"]["stale"]:
                flags.append("STALE")
            if window.get("time_anomaly"):
                flags.append("TIME ANOMALY")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(
                f"  {name}  {_percent(window['remaining']['display_percent'])}"
                f"  {reset}  {window['sampled_at']}  {window['machine']}{suffix}"
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="JSON config path")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--tz", help="report timezone (IANA name)")
    parser.add_argument("--now", help="override the current UTC time (RFC3339)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m telemetre.quota",
        description=(
            "Sample agy and Codex subscription quota windows, store sanitized "
            "observations locally, and print current or dated reports."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser(
        "sample", help="read configured providers and store this observation"
    )
    _add_common(sample)
    sample.add_argument(
        "--if-due",
        action="store_true",
        help="skip sources whose previous attempt is younger than their interval",
    )
    sample.set_defaults(handler=_cmd_sample)

    report = subparsers.add_parser(
        "report", help="print the current quota state or a dated history"
    )
    _add_common(report)
    report.add_argument("--date", help="YYYY-MM-DD in the report timezone")
    report.add_argument("--check", action="store_true", help="exit non-zero on stale or missing sources")
    report.set_defaults(handler=_cmd_report)

    daily = subparsers.add_parser(
        "daily", help="build and store a dated daily quota summary"
    )
    _add_common(daily)
    daily.add_argument("--date", help="YYYY-MM-DD in the report timezone")
    daily.set_defaults(handler=_cmd_daily)

    return parser


def _resolve_database(args, config: Config | None) -> str:
    if args.db:
        return args.db
    if config is not None and config.database:
        return config.database
    if os.environ.get("TELEMETRE_QUOTA_DB"):
        return os.environ["TELEMETRE_QUOTA_DB"]
    return DEFAULT_DATABASE


def _resolve_timezone(args, config: Config | None) -> str:
    return args.tz or (config.timezone if config else "UTC")


def _cmd_sample(args) -> int:
    config = load_config(args.config)
    database = _resolve_database(args, config)
    now = _now(args.now)
    outcomes = []
    failed = 0
    connection = connect_database(database)
    try:
        for source_config in config.sources:
            if args.if_due:
                previous = connection.execute(
                    """
                    SELECT sampled_epoch FROM quota_samples
                    WHERE provider = ? AND account = ? AND machine = ?
                    ORDER BY sampled_epoch DESC LIMIT 1
                    """,
                    (source_config.provider, source_config.account, config.machine),
                ).fetchone()
                if previous is not None:
                    elapsed = now.timestamp() - float(previous["sampled_epoch"])
                    if elapsed < source_config.interval_seconds:
                        outcomes.append(
                            {
                                "provider": source_config.provider,
                                "account": source_config.account,
                                "skipped": True,
                                "reason": "not_due",
                            }
                        )
                        continue
            observation = collect_observation(
                source_config, machine=config.machine, sampled_at=now
            )
            ingest_observation(database, observation, received_at=now)
            outcomes.append(observation.to_dict())
            if observation.status != "success":
                failed += 1
    finally:
        connection.close()
    payload = {
        "schema": OBSERVATION_SCHEMA,
        "sampled_at": format_timestamp(now),
        "sampled": outcomes,
        "failed": failed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for outcome in outcomes:
            if outcome.get("skipped"):
                print(f"{outcome['provider']}/{outcome['account']}: not due")
            elif outcome["status"] == "success":
                print(
                    f"{outcome['provider']}/{outcome['account']}: success, "
                    f"{len(outcome['windows'])} window(s)"
                )
            else:
                print(
                    f"{outcome['provider']}/{outcome['account']}: FAILURE "
                    f"({outcome['error']['kind']})"
                )
    return 1 if failed else 0


def _cmd_report(args) -> int:
    config = _optional_config(args)
    database = _resolve_database(args, config)
    if args.date:
        report = date_report(
            database,
            args.date,
            config=config,
            now=args.now,
            tz=_resolve_timezone(args, config),
        )
    else:
        report = current_report(
            database, config=config, now=args.now, tz=_resolve_timezone(args, config)
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_current(report) if report["mode"] == "current" else render_date(report), end="")
    if args.check:
        unhealthy = [
            account
            for account in report["accounts"]
            if account.get("missing") or account.get("stale")
            or (
                account.get("last_attempt") is not None
                and account["last_attempt"]["status"] == "failure"
            )
        ]
        return 3 if unhealthy else 0
    return 0


def _optional_config(args) -> Config | None:
    environment_path = Path(
        os.environ.get("TELEMETRE_QUOTA_CONFIG", DEFAULT_CONFIG)
    ).expanduser()
    if args.config or environment_path.exists():
        return load_config(args.config)
    return None


def _cmd_daily(args) -> int:
    config = _optional_config(args)
    database = _resolve_database(args, config)
    timezone_name = _resolve_timezone(args, config)
    if args.date:
        day = args.date
    else:
        generated = _now(args.now)
        day = (generated.astimezone(_zone(timezone_name)).date() - timedelta(days=1)).isoformat()
    report = build_daily_summary(
        database, day, config=config, now=args.now, tz=timezone_name
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_date(report), end="")
    return 0


def _date_range(day: str, tz_name: str, now: Any = None):
    try:
        parsed_day = date_type.fromisoformat(day)
    except ValueError:
        raise QuotaError(f"invalid date: {day}") from None
    zone = _zone(tz_name)
    start_local = datetime.combine(parsed_day, time_type.min, tzinfo=zone)
    calendar_end_local = start_local + timedelta(days=1)
    start = start_local.astimezone(UTC)
    calendar_end = calendar_end_local.astimezone(UTC)
    generated = _now(now)
    effective_end = calendar_end if generated >= calendar_end else max(start, generated)
    return parsed_day, start, effective_end, calendar_end, generated


def _missing_gaps(
    attempt_epochs: Sequence[float],
    start: datetime,
    end: datetime,
    interval_seconds: int,
) -> list[dict]:
    threshold = STALE_INTERVALS * interval_seconds
    gaps: list[dict] = []

    def add_gap(from_dt: datetime, to_dt: datetime) -> None:
        seconds = int((to_dt - from_dt).total_seconds())
        if seconds > threshold:
            gaps.append(
                {
                    "from": format_timestamp(from_dt),
                    "to": format_timestamp(to_dt),
                    "seconds": seconds,
                }
            )

    if not attempt_epochs:
        add_gap(start, end)
        return gaps
    first = datetime.fromtimestamp(attempt_epochs[0], tz=UTC)
    add_gap(start, first)
    previous = first
    for epoch in attempt_epochs[1:]:
        current = datetime.fromtimestamp(epoch, tz=UTC)
        add_gap(previous, current)
        previous = current
    add_gap(previous, end)
    return gaps


def _date_window(
    identity: tuple, observations: list[dict], interval_seconds: int
) -> dict:
    first = observations[0]
    last = observations[-1]
    known = [item for item in observations if item["display"] is not None]
    minimum = None
    if known:
        minimum_item = min(known, key=lambda item: (item["display"], item["epoch"]))
        minimum = {
            "display_percent": minimum_item["display"],
            "sampled_at": minimum_item["sampled_at"],
            "known": True,
        }
    else:
        minimum = {"display_percent": None, "sampled_at": None, "known": False}

    segments = 0
    zero_state: bool | None = None
    previous_epoch: float | None = None
    has_gap = False
    for item in observations:
        epoch = item["epoch"]
        display = item["display"]
        gap = (
            previous_epoch is not None
            and epoch - previous_epoch > STALE_INTERVALS * interval_seconds
        )
        if gap:
            has_gap = True
        previous_epoch = epoch
        if display is None:
            continue
        is_zero = display <= 0.0
        if gap:
            if is_zero:
                if zero_state is not True:
                    segments += 1
                zero_state = True
            else:
                zero_state = False
        elif is_zero:
            if zero_state is not True:
                segments += 1
            zero_state = True
        else:
            zero_state = False

    anomalies = sorted(
        {
            anomaly
            for item in observations
            for anomaly in item["anomalies"]
        }
    )
    return {
        "group": last["group"],
        "bucket_id": last["bucket_id"],
        "limit_id": last["limit_id"],
        "slot": last["slot"],
        "window": last["window"],
        "window_minutes": last["window_minutes"],
        "window_display": _duration_display(last["window_minutes"]),
        "reset": {
            "raw": last["reset_raw"],
            "at": last["reset_at"],
            "unknown": last["reset_at"] is None,
        },
        "minimum": minimum,
        "latest": {
            "display_percent": last["display"],
            "sampled_at": last["sampled_at"],
            "machine": last["machine"],
            "source_value": last["remaining_value"],
            "source_unit": last["remaining_unit"],
        },
        "bottom_segments": segments,
        "observations": len(observations),
        "has_gap": has_gap,
        "anomalies": anomalies,
        "time_anomaly": any(item["time_anomaly"] for item in observations),
    }


def date_report(
    database: str | os.PathLike,
    day: str,
    *,
    config: Config | None = None,
    now: Any = None,
    tz: str | None = None,
) -> dict:
    timezone_name = tz or (config.timezone if config else "UTC")
    parsed_day, start, end, calendar_end, generated = _date_range(
        day, timezone_name, now
    )
    configured = _source_identity(config.sources) if config else {}
    connection = connect_database(database)
    try:
        accounts = list(configured)
        for identity in _database_accounts(connection):
            if identity not in accounts:
                accounts.append(identity)
        result_accounts = []
        start_epoch = start.timestamp()
        end_epoch = end.timestamp()
        for provider, account in accounts:
            interval = configured.get((provider, account))
            latest_success_before_end = connection.execute(
                """
                SELECT * FROM quota_samples
                WHERE provider = ? AND account = ? AND status = 'success'
                  AND sampled_epoch < ?
                ORDER BY sampled_epoch DESC, received_epoch DESC, sample_id DESC
                LIMIT 1
                """,
                (provider, account, end_epoch),
            ).fetchone()
            if interval is None and latest_success_before_end is not None:
                interval = int(latest_success_before_end["interval_seconds"])
            interval = interval or DEFAULT_INTERVAL_SECONDS
            rows = connection.execute(
                """
                SELECT * FROM quota_samples
                WHERE provider = ? AND account = ?
                  AND sampled_epoch >= ? AND sampled_epoch < ?
                ORDER BY sampled_epoch ASC, received_epoch ASC, sample_id ASC
                """,
                (provider, account, start_epoch, end_epoch),
            ).fetchall()
            success_rows = [row for row in rows if row["status"] == "success"]
            last_attempt_row = rows[-1] if rows else None
            last_success_row = success_rows[-1] if success_rows else None
            gaps = _missing_gaps(
                [float(row["sampled_epoch"]) for row in rows],
                start,
                end,
                interval,
            )
            last_attempt = None
            if last_attempt_row is not None:
                last_attempt = {
                    "sample_id": last_attempt_row["sample_id"],
                    "status": last_attempt_row["status"],
                    "sampled_at": last_attempt_row["sampled_at"],
                    "received_at": last_attempt_row["received_at"],
                    "machine": last_attempt_row["machine"],
                    "error_kind": last_attempt_row["error_kind"],
                    "error_detail": last_attempt_row["error_detail"],
                    "time_anomaly": bool(last_attempt_row["time_anomaly"]),
                }

            grouped: dict[tuple, list[dict]] = {}
            for row in success_rows:
                for window in _windows_for_sample(connection, row["sample_id"]):
                    identity = (
                        window["group_name"],
                        window["bucket_id"],
                        window["limit_id"],
                        window["slot"],
                        window["reset_at"]
                        if window["reset_at"] is not None
                        else _json_dump(_json_load(window["reset_raw"])),
                        window["window_minutes"],
                    )
                    grouped.setdefault(identity, []).append(
                        {
                            "epoch": float(row["sampled_epoch"]),
                            "sampled_at": row["sampled_at"],
                            "machine": row["machine"],
                            "group": window["group_name"],
                            "bucket_id": window["bucket_id"],
                            "limit_id": window["limit_id"],
                            "slot": window["slot"],
                            "window": _json_load(window["window_raw"]),
                            "window_minutes": window["window_minutes"],
                            "reset_raw": _json_load(window["reset_raw"]),
                            "reset_at": window["reset_at"],
                            "remaining_value": window["remaining_value"],
                            "remaining_unit": window["remaining_unit"],
                            "display": window["remaining_display"],
                            "anomalies": _json_load(window["anomalies"], []),
                            "time_anomaly": bool(row["time_anomaly"]),
                        }
                    )
            windows = [
                _date_window(identity, observations, interval)
                for identity, observations in sorted(
                    grouped.items(),
                    key=lambda item: tuple(
                        "" if part is None else str(part) for part in item[0]
                    ),
                )
            ]
            empty_windows = bool(
                last_success_row is not None and last_success_row["empty_windows"]
            ) and not windows
            stale = True
            if latest_success_before_end is not None:
                age = max(
                    0,
                    int(
                        (
                            generated
                            - _timestamp_from_any(latest_success_before_end["sampled_at"])
                        ).total_seconds()
                    ),
                )
                stale = age > STALE_INTERVALS * interval
            result_accounts.append(
                {
                    "provider": provider,
                    "account": account,
                    "interval_seconds": interval,
                    "missing": bool(gaps),
                    "missing_gaps": gaps,
                    "stale": stale,
                    "attempts": len(rows),
                    "failures": len(rows) - len(success_rows),
                    "last_attempt": last_attempt,
                    "last_success_at": (
                        latest_success_before_end["sampled_at"]
                        if latest_success_before_end is not None
                        else None
                    ),
                    "last_success_in_range": (
                        last_success_row["sampled_at"]
                        if last_success_row is not None
                        else None
                    ),
                    "empty_windows": empty_windows,
                    "windows": windows,
                }
            )
        return {
            "schema": REPORT_SCHEMA,
            "mode": "date",
            "date": parsed_day.isoformat(),
            "timezone": timezone_name,
            "generated_at": format_timestamp(generated),
            "range": {
                "start": format_timestamp(start),
                "end": format_timestamp(end),
                "calendar_end": format_timestamp(calendar_end),
                "complete": end >= calendar_end,
            },
            "accounts": result_accounts,
        }
    finally:
        connection.close()


def build_daily_summary(
    database: str | os.PathLike,
    day: str,
    *,
    config: Config | None = None,
    now: Any = None,
    tz: str | None = None,
) -> dict:
    report = date_report(database, day, config=config, now=now, tz=tz)
    report = dict(report)
    report["mode"] = "daily"
    report["stored"] = True
    connection = connect_database(database)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO quota_daily (day, timezone, generated_at, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day, timezone) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    payload = excluded.payload
                """,
                (
                    report["date"],
                    report["timezone"],
                    report["generated_at"],
                    json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                ),
            )
    finally:
        connection.close()
    return report


def get_daily_summary(
    database: str | os.PathLike, day: str, tz: str
) -> dict | None:
    connection = connect_database(database)
    try:
        row = connection.execute(
            "SELECT payload FROM quota_daily WHERE day = ? AND timezone = ?",
            (day, tz),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])
    finally:
        connection.close()


def render_date(report: Mapping[str, Any]) -> str:
    title = "Daily quota summary" if report["mode"] == "daily" else "Quota report (date)"
    lines = [
        f"{title} · {report['date']} · timezone {report['timezone']} · "
        f"UTC {report['range']['start']} .. {report['range']['end']}"
    ]
    if not report["range"]["complete"]:
        lines.append("  (partial day; range ends at report time)")
    for account in report["accounts"]:
        latest = account["last_attempt"]
        if latest is None:
            state = "no sample in range"
        elif latest["status"] == "success":
            state = f"last attempt success at {latest['sampled_at']}"
        else:
            state = f"last attempt FAILURE ({latest['error_kind']}) at {latest['sampled_at']}"
        flags = []
        if account["missing"]:
            flags.append("MISSING GAPS")
        if account["stale"]:
            flags.append("STALE (>2 intervals)")
        lines.append(
            f"\n{account['provider']}/{account['account']}  {state}"
            + (f"  [{', '.join(flags)}]" if flags else "")
        )
        lines.append(
            f"  last success in range: {account['last_success_in_range'] or 'none'}; "
            f"last known success: {account['last_success_at'] or 'none'}"
        )
        for gap in account["missing_gaps"]:
            lines.append(f"  missing: {gap['from']} .. {gap['to']}")
        if not account["windows"]:
            lines.append(
                "  no windows returned"
                if account["empty_windows"]
                else "  no successful window snapshot in range"
            )
            continue
        lines.append(
            "  group / bucket / window   minimum   latest   bottom segments   observations"
        )
        for window in account["windows"]:
            name = f"{window['group']} / {window['bucket_id']}"
            if window["window_display"]:
                name += f" / {window['window_display']}"
            if window["reset"]["at"]:
                name += f" / reset {window['reset']['at']}"
            minimum = window["minimum"]
            min_text = _percent(minimum["display_percent"])
            if minimum["sampled_at"]:
                min_text += f" at {minimum['sampled_at']}"
            latest = window["latest"]
            latest_text = _percent(latest["display_percent"])
            if latest["sampled_at"]:
                latest_text += f" at {latest['sampled_at']}"
            suffix = " [GAP]" if window["has_gap"] else ""
            if window.get("time_anomaly"):
                suffix += " [TIME ANOMALY]"
            lines.append(
                f"  {name}  min {min_text}  latest {latest_text}"
                f"  bottom segments {window['bottom_segments']}"
                f"  observations {window['observations']}{suffix}"
            )
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except QuotaError as exc:
        print(f"telemetre.quota: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"telemetre.quota: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"telemetre.quota: database error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("telemetre.quota: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
