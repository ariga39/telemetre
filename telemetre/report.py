"""Monthly agy usage report over the centre database."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .db import connect, init_center_schema
from .protocol import COUNT_FIELDS
from .timeutil import month_utc_range, utc_text


def _empty_row() -> dict[str, Any]:
    row: dict[str, Any] = {field: 0 for field in COUNT_FIELDS}
    row["model_steps"] = 0
    return row


def _add_event(row: dict[str, Any], event: Any) -> None:
    row["model_steps"] += 1
    for field in COUNT_FIELDS:
        value = event[field]
        if value is not None:
            row[field] += int(value)


def _aggregate(events: Iterable[Any]) -> dict[str, Any]:
    row = _empty_row()
    for event in events:
        _add_event(row, event)
    return row


def _sources_for_event(conn: Any, account: str, conversation_id: str, step_index: int) -> list[str]:
    rows = conn.execute(
        "SELECT machine FROM event_sources WHERE account = ? AND conversation_id = ? "
        "AND step_index = ? ORDER BY machine",
        (account, conversation_id, step_index),
    ).fetchall()
    return [row["machine"] for row in rows]


def _sources_for_account(conn: Any, account: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM machine_status WHERE account = ? ORDER BY machine",
        (account,),
    ).fetchall()
    sources: list[dict[str, Any]] = []
    for row in rows:
        scan: dict[str, Any] = {}
        if row["last_scan_json"]:
            try:
                scan = json.loads(row["last_scan_json"])
            except json.JSONDecodeError:
                scan = {}
        sources.append(
            {
                "machine": row["machine"],
                "account": row["account"],
                "kind": row["kind"],
                "last_success_at": row["last_success_at"],
                "collector_version": row["collector_version"],
                "bad_lines": scan.get("bad_lines", 0),
                "files_seen": scan.get("files_seen", 0),
                "files_read": scan.get("files_read", 0),
                "missing_count_steps": scan.get("missing_count_steps", 0),
                "pending_backlog": scan.get("pending_backlog", 0),
                "source_missing": bool(scan.get("source_missing", 0)),
                "source_errors": scan.get("source_errors", 0),
                "run_id": row["last_run_id"],
            }
        )
    return sources


def _conflict_evidence(conn: Any, account: str, event: Any) -> dict[str, Any]:
    observations = conn.execute(
        "SELECT machine, digest, event_time, model, input_tokens, cache_read_tokens, "
        "output_tokens, collector_version, received_at FROM observations "
        "WHERE account = ? AND conversation_id = ? AND step_index = ? "
        "ORDER BY received_at, machine, digest",
        (account, event["conversation_id"], event["step_index"]),
    ).fetchall()
    return {
        "conversation_id": event["conversation_id"],
        "step_index": event["step_index"],
        "model": event["model"],
        "event_time": event["event_time"],
        "observations": [
            {
                "machine": row["machine"],
                "digest": row["digest"],
                "event_time": row["event_time"],
                "model": row["model"],
                "input_tokens": row["input_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "output_tokens": row["output_tokens"],
                "collector_version": row["collector_version"],
                "received_at": row["received_at"],
            }
            for row in observations
        ],
    }


def build_report(center_db: str | Path, *, month: str, tz_name: str = "UTC") -> dict[str, Any]:
    start_us, end_us, _, _ = month_utc_range(month, tz_name)
    conn = connect(center_db)
    try:
        init_center_schema(conn)
        events = conn.execute(
            "SELECT * FROM events WHERE event_us >= ? AND event_us < ? "
            "ORDER BY account, machine, model, event_us, conversation_id, step_index",
            (start_us, end_us),
        ).fetchall()
        observation_conflicts = conn.execute(
            "SELECT DISTINCT e.account, e.conversation_id, e.step_index, e.event_us, "
            "e.event_time, e.model, e.input_tokens, e.cache_read_tokens, e.output_tokens, "
            "e.status, e.machine, e.created_at, e.updated_at "
            "FROM events e JOIN observations o ON e.account = o.account "
            "AND e.conversation_id = o.conversation_id AND e.step_index = o.step_index "
            "WHERE e.status = 'conflict' AND o.event_us >= ? AND o.event_us < ?",
            (start_us, end_us),
        ).fetchall()
        status_rows = conn.execute(
            "SELECT DISTINCT account FROM machine_status ORDER BY account"
        ).fetchall()
        all_sources = conn.execute(
            "SELECT * FROM machine_status ORDER BY account, machine"
        ).fetchall()

        conflict_by_key = {
            (row["account"], row["conversation_id"], row["step_index"]): row
            for row in events
            if row["status"] == "conflict"
        }
        for row in observation_conflicts:
            conflict_by_key[(row["account"], row["conversation_id"], row["step_index"])] = row

        accounts: dict[str, Any] = {}
        account_names = (
            {row["account"] for row in events}
            | {row["account"] for row in conflict_by_key.values()}
            | {row["account"] for row in status_rows}
        )
        for account in sorted(account_names):
            known_events = [
                event
                for event in events
                if event["account"] == account and event["status"] != "conflict"
            ]
            conflict_events = sorted(
                (
                    row
                    for (row_account, _, _), row in conflict_by_key.items()
                    if row_account == account
                ),
                key=lambda row: (row["conversation_id"], row["step_index"]),
            )
            ambiguous_events = [event for event in known_events if event["status"] == "ambiguous"]

            machine_models: dict[tuple[str, str], dict[str, Any]] = {}
            for event in known_events:
                if event["status"] != "ok":
                    continue
                key = (event["machine"], event["model"])
                row = machine_models.setdefault(key, _empty_row())
                _add_event(row, event)

            machines: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for (machine, model), row in sorted(machine_models.items()):
                machines[machine].append({"model": model, **row})

            unattributed: dict[str, dict[str, Any]] = {}
            unattributed_sources: dict[str, set[str]] = defaultdict(set)
            for event in ambiguous_events:
                row = unattributed.setdefault(event["model"], _empty_row())
                _add_event(row, event)
                unattributed_sources[event["model"]].update(
                    _sources_for_event(
                        conn,
                        account,
                        event["conversation_id"],
                        event["step_index"],
                    )
                )

            missing = {
                field: sum(1 for event in known_events if event[field] is None)
                for field in COUNT_FIELDS
            }
            coverage = {
                "steps": len(known_events),
                "missing": missing,
                "reasoning": "unknown",
                "note": "reasoning is not collected and is never estimated from message text",
            }
            accounts[account] = {
                "machines": [
                    {"machine": machine, "models": rows}
                    for machine, rows in sorted(machines.items())
                ],
                "unattributed": [
                    {
                        "model": model,
                        **row,
                        "sources": sorted(unattributed_sources[model]),
                    }
                    for model, row in sorted(unattributed.items())
                ],
                "account_total": _aggregate(known_events),
                "coverage": coverage,
                "conflicts": [
                    _conflict_evidence(conn, account, event) for event in conflict_events
                ],
                "sources": _sources_for_account(conn, account),
            }
    finally:
        conn.close()

    sources = []
    for row in all_sources:
        scan: dict[str, Any] = {}
        if row["last_scan_json"]:
            try:
                scan = json.loads(row["last_scan_json"])
            except json.JSONDecodeError:
                scan = {}
        sources.append(
            {
                "machine": row["machine"],
                "account": row["account"],
                "kind": row["kind"],
                "last_success_at": row["last_success_at"],
                "collector_version": row["collector_version"],
                "bad_lines": scan.get("bad_lines", 0),
                "files_seen": scan.get("files_seen", 0),
                "files_read": scan.get("files_read", 0),
                "missing_count_steps": scan.get("missing_count_steps", 0),
                "pending_backlog": scan.get("pending_backlog", 0),
                "source_missing": bool(scan.get("source_missing", 0)),
                "source_errors": scan.get("source_errors", 0),
                "run_id": row["last_run_id"],
            }
        )

    return {
        "kind": "agy_usage_report",
        "month": month,
        "timezone": tz_name,
        "utc_start": utc_text(start_us),
        "utc_end": utc_text(end_us),
        "source_version": "unknown",
        "fork_semantics": "unverified",
        "limitations": [
            "offline or unconfigured devices cannot be inferred",
            "server-side background calls and requests that never reach disk cannot be inferred",
        ],
        "accounts": accounts,
        "sources": sources,
    }


def _fmt(value: int) -> str:
    return f"{value:,}"


def _render_rows(rows: list[dict[str, Any]], label: str) -> list[str]:
    lines: list[str] = []
    for row in rows:
        suffix = ""
        if row.get("sources"):
            suffix = "  sources=" + ",".join(row["sources"])
        lines.append(
            f"  {label:<15} {row['model']:<24} {_fmt(row['input_tokens']):>14} "
            f"{_fmt(row['cache_read_tokens']):>18} {_fmt(row['output_tokens']):>14} "
            f"{row['model_steps']:>6}{suffix}"
        )
    return lines


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "agy usage report",
        f"month: {report['month']} ({report['timezone']})",
        f"UTC range: [{report['utc_start']}, {report['utc_end']}) left-closed, right-open",
        "",
    ]
    accounts = report.get("accounts", {})
    if not accounts:
        lines.append("no events and no source status for this month")
    for account, data in accounts.items():
        lines.append(f"account: {account}")
        lines.append(
            f"  {'machine':<15} {'model':<24} {'input_tokens':>14} "
            f"{'cache_read_tokens':>18} {'output_tokens':>14} {'steps':>6}"
        )
        for machine_block in data["machines"]:
            lines.extend(_render_rows(machine_block["models"], machine_block["machine"]))
        lines.extend(_render_rows(data["unattributed"], "unattributed"))
        total = data["account_total"]
        lines.append(
            f"  {'account total':<15} {'':<24} {_fmt(total['input_tokens']):>14} "
            f"{_fmt(total['cache_read_tokens']):>18} {_fmt(total['output_tokens']):>14} "
            f"{total['model_steps']:>6}"
        )
        coverage = data["coverage"]
        missing = coverage["missing"]
        lines.append(
            f"  coverage: steps={coverage['steps']} "
            f"missing input_tokens={missing['input_tokens']} "
            f"cache_read_tokens={missing['cache_read_tokens']} "
            f"output_tokens={missing['output_tokens']}; reasoning=unknown"
        )
        conflicts = data["conflicts"]
        lines.append(f"  conflicts: {len(conflicts)} (excluded from known totals)")
        for conflict in conflicts:
            lines.append(
                f"    conversation={conflict['conversation_id']} step={conflict['step_index']} "
                f"model={conflict['model']} evidence={len(conflict['observations'])}"
            )
        lines.append("")

    lines.append("sources")
    if not report.get("sources"):
        lines.append("  none reported")
    for source in report.get("sources", []):
        lines.append(
            f"  {source['machine']}/{source['account']}: last successful scan "
            f"{source['last_success_at'] or 'never'}, "
            f"files_seen={source['files_seen']}, files_read={source['files_read']}, "
            f"bad_lines={source['bad_lines']}, "
            f"missing_count_steps={source['missing_count_steps']}, "
            f"pending_backlog={source['pending_backlog']}, "
            f"source_errors={source['source_errors']}, "
            f"source_missing={str(source['source_missing']).lower()}"
        )
    lines.append("")
    lines.append("reasoning coverage: unknown (not collected; never inferred from message text)")
    lines.append(
        "source version: unknown (agy logs do not label a production version; collector "
        "versions are shown per source)"
    )
    lines.append(
        "fork/rewind semantics: unverified; identical copied steps dedupe, reused step "
        "indices with different counts are isolated as conflicts"
    )
    for limitation in report.get("limitations", []):
        lines.append(f"limitation: {limitation}")
    return "\n".join(lines)
