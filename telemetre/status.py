"""Source-status check used by external monitoring."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .db import connect, init_center_schema
from .timeutil import parse_rfc3339, utc_text


def check_status(
    center_db: str | Path,
    *,
    expected: Iterable[tuple[str, str]] = (),
    max_age_seconds: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expected_list = list(expected)
    conn = connect(center_db)
    try:
        init_center_schema(conn)
        rows = conn.execute(
            "SELECT * FROM machine_status ORDER BY account, machine"
        ).fetchall()
    finally:
        conn.close()

    by_key = {(row["machine"], row["account"]): row for row in rows}
    checks: list[dict[str, Any]] = []
    ok = True

    targets = expected_list if expected_list else [(row["machine"], row["account"]) for row in rows]
    for machine, account in targets:
        row = by_key.get((machine, account))
        if row is None:
            checks.append(
                {
                    "machine": machine,
                    "account": account,
                    "ok": False,
                    "reason": "no successful scan reported",
                }
            )
            ok = False
            continue
        scan: dict[str, Any] = {}
        if row["last_scan_json"]:
            try:
                scan = json.loads(row["last_scan_json"])
            except json.JSONDecodeError:
                scan = {}
        last_success = row["last_success_at"]
        age_seconds: int | None = None
        reason = "ok"
        if last_success:
            try:
                age_seconds = int((now - parse_rfc3339(last_success)).total_seconds())
            except ValueError:
                reason = "invalid last_success_at"
        else:
            reason = "no successful scan reported"
        if scan.get("source_missing"):
            reason = "expected source missing"
        elif int(scan.get("source_errors", 0) or 0) > 0:
            reason = "source read/traversal errors"
        if max_age_seconds is not None and age_seconds is not None and age_seconds > max_age_seconds:
            reason = "stale scan"
        if reason != "ok":
            ok = False
        checks.append(
            {
                "machine": machine,
                "account": account,
                "ok": reason == "ok",
                "reason": reason,
                "last_success_at": last_success,
                "age_seconds": age_seconds,
                "files_seen": scan.get("files_seen", 0),
                "files_read": scan.get("files_read", 0),
                "bad_lines": scan.get("bad_lines", 0),
                "missing_count_steps": scan.get("missing_count_steps", 0),
                "pending_backlog": scan.get("pending_backlog", 0),
                "source_missing": bool(scan.get("source_missing", 0)),
                "source_errors": scan.get("source_errors", 0),
            }
        )

    return {
        "ok": ok,
        "checked_at": utc_text(now),
        "max_age_seconds": max_age_seconds,
        "checks": checks,
    }


def render_status_text(status: dict[str, Any]) -> str:
    lines = [
        f"status checked at {status['checked_at']}",
        f"overall: {'ok' if status['ok'] else 'attention required'}",
    ]
    for check in status["checks"]:
        state = "ok" if check["ok"] else f"FAIL ({check['reason']})"
        lines.append(
            f"  {check['machine']}/{check['account']}: "
            f"{state} "
            f"last_success={check.get('last_success_at') or 'never'} "
            f"files_seen={check.get('files_seen', 0)} "
            f"files_read={check.get('files_read', 0)} "
            f"bad_lines={check.get('bad_lines', 0)} "
            f"missing_count_steps={check.get('missing_count_steps', 0)} "
            f"pending_backlog={check.get('pending_backlog', 0)} "
            f"source_errors={check.get('source_errors', 0)}"
        )
    return "\n".join(lines)
