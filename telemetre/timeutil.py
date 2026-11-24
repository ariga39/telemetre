"""UTC / calendar-month helpers shared by the collector and reports."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339-like timestamp and return an aware UTC datetime.

    A timestamp without an offset is treated as UTC.  agy writes offset
    timestamps; accepting a naive value keeps synthetic/older fixtures usable
    without ever inventing a local offset.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid RFC3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_us(value: datetime) -> int:
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_us(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def utc_text(value: datetime | int) -> str:
    moment = from_us(value) if isinstance(value, int) else value.astimezone(timezone.utc)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_month(value: str) -> tuple[int, int]:
    match = _MONTH_RE.match(value or "")
    if not match:
        raise ValueError("month must be in YYYY-MM form")
    year, month = int(match.group(1)), int(match.group(2))
    if month < 1 or month > 12:
        raise ValueError("month must be in YYYY-MM form")
    return year, month


def month_utc_range(month: str, tz_name: str = "UTC") -> tuple[int, int, datetime, datetime]:
    """Return [start, end) as UTC microseconds for a local calendar month.

    Month selection is the calendar month in ``tz_name``; events themselves are
    always stored and compared in UTC.  This is the DESIGN.md rule for --tz.
    """
    year, month_number = parse_month(month)
    try:
        zone = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError and platform errors
        raise ValueError(f"unknown timezone: {tz_name!r}") from exc
    start_local = datetime(year, month_number, 1, tzinfo=zone)
    if month_number == 12:
        next_local = datetime(year + 1, 1, 1, tzinfo=zone)
    else:
        next_local = datetime(year, month_number + 1, 1, tzinfo=zone)
    return (
        to_us(start_local.astimezone(timezone.utc)),
        to_us(next_local.astimezone(timezone.utc)),
        start_local,
        next_local,
    )
