"""Operator health check for expected usage and quota sources.

Usage health stays on the usage slice's ``status.check_status`` API.  Quota
health uses the quota slice's public ``current_report`` view and adds the
machine expectation, which is what detects a configured source that never
reported instead of silently showing an empty table.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import quota
from .status import check_status
from .timeutil import utc_text

UsageExpectation = tuple[str, str]
QuotaExpectation = tuple[str, str, str]


def _quota_checks(
    center_db: str | Path,
    expected: Iterable[QuotaExpectation],
    *,
    max_age_seconds: int | None,
    now: datetime,
) -> list[dict[str, Any]]:
    report = quota.current_report(center_db, now=now, tz="UTC")
    by_key = {
        (account["provider"], account["account"]): account
        for account in report.get("accounts", [])
    }
    checks: list[dict[str, Any]] = []
    for machine, provider, account_name in dict.fromkeys(expected):
        account = by_key.get((provider, account_name))
        if account is None:
            checks.append(
                {
                    "kind": "quota_sample",
                    "machine": machine,
                    "provider": provider,
                    "account": account_name,
                    "ok": False,
                    "reason": "no successful scan reported",
                }
            )
            continue
        latest = account.get("last_attempt")
        success = account.get("last_success")
        age = account.get("age_seconds")
        reason = "ok"
        if account.get("missing"):
            reason = "no successful scan reported"
        elif latest is not None and latest.get("status") == "failure":
            reason = f"latest attempt failed: {latest.get('error_kind') or 'unknown'}"
        elif success is not None and latest is not None and latest.get("machine") != machine:
            reason = "latest sample came from a different machine"
        elif success is not None and success.get("machine") != machine:
            reason = "latest successful sample came from a different machine"
        elif max_age_seconds is not None:
            if age is not None and age > max_age_seconds:
                reason = "stale sample"
        elif account.get("stale"):
            reason = "stale sample"
        checks.append(
            {
                "kind": "quota_sample",
                "machine": machine,
                "provider": provider,
                "account": account_name,
                "ok": reason == "ok",
                "reason": reason,
                "last_attempt": latest,
                "last_success": success,
                "age_seconds": age,
                "interval_seconds": account.get("interval_seconds"),
            }
        )
    return checks


def check_health(
    center_db: str | Path,
    *,
    expected_usage: Iterable[UsageExpectation] = (),
    expected_quota: Iterable[QuotaExpectation] = (),
    max_age_seconds: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    usage = check_status(
        center_db,
        expected=list(dict.fromkeys(expected_usage)),
        max_age_seconds=max_age_seconds,
        now=moment,
    )
    checks: list[dict[str, Any]] = []
    for check in usage["checks"]:
        item = dict(check)
        item["kind"] = "agy_usage"
        checks.append(item)
    checks.extend(
        _quota_checks(
            center_db,
            expected_quota,
            max_age_seconds=max_age_seconds,
            now=moment,
        )
    )
    return {
        "ok": bool(checks) and all(item["ok"] for item in checks),
        "checked_at": utc_text(moment),
        "max_age_seconds": max_age_seconds,
        "checks": checks,
    }


def render_text(status: dict[str, Any]) -> str:
    lines = [
        f"health checked at {status['checked_at']}",
        f"overall: {'ok' if status['ok'] else 'attention required'}",
    ]
    for check in status["checks"]:
        state = "ok" if check["ok"] else f"FAIL ({check['reason']})"
        if check["kind"] == "agy_usage":
            lines.append(
                f"  agy_usage {check['machine']}/{check['account']}: {state} "
                f"last_success={check.get('last_success_at') or 'never'}"
            )
        else:
            lines.append(
                f"  quota {check['provider']}/{check['account']} on {check['machine']}: {state} "
                f"last_success={(check.get('last_success') or {}).get('sampled_at') or 'never'}"
            )
    return "\n".join(lines)


__all__ = ["check_health", "render_text"]
