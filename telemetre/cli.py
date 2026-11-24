"""Command-line entry point for the agy usage vertical slice."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .collector import CollectorError, collect
from .protocol import IngestContext, ProtocolError
from .replay import (
    LocalCenterTransport,
    ReplayError,
    build_next_batch,
    replay_pending,
    spool_identity,
)
from .receiver import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, receive
from .report import build_report, render_text as render_report_text
from .status import check_status, render_status_text

DEFAULT_TZ = "UTC"


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _add_format(parser: argparse.ArgumentParser, *, default: str = "text") -> None:
    parser.add_argument("--format", choices=("text", "json"), default=default)


def _add_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)


def _parse_expected(values: list[str]) -> list[tuple[str, str]]:
    expected: list[tuple[str, str]] = []
    for value in values:
        machine, separator, account = value.partition(":")
        if not separator or not machine or not account:
            raise ValueError("--expected must be machine:account")
        expected.append((machine, account))
    return expected


def _cmd_collect(args: argparse.Namespace) -> int:
    result = collect(
        args.client_db,
        machine=args.machine,
        account=args.account,
        roots=args.root,
        collector_version=args.collector_version,
    )
    if args.format == "json":
        _print_json(result)
    else:
        print(
            f"collected {result['new_facts']} new / {result['changed_facts']} changed facts "
            f"from {result['files_read']} read files "
            f"({result['files_skipped']} unchanged); bad_lines={result['bad_lines']}, "
            f"missing_count_steps={result['missing_count_steps']}, "
            f"pending_backlog={result['pending_backlog']}, "
            f"source_missing={str(result['source_missing']).lower()}"
        )
    return 0


def _cmd_pending(args: argparse.Namespace) -> int:
    batch = build_next_batch(
        args.client_db,
        max_rows=args.max_rows,
        max_bytes=args.max_bytes,
    )
    if args.format == "json":
        sys.stdout.buffer.write(batch.payload + b"\n")
        sys.stdout.buffer.flush()
    else:
        print(
            f"next batch: {batch.fact_count} facts, {len(batch.payload)} bytes, "
            f"{len(batch.pending_ids)} pending rows"
        )
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    identity = spool_identity(args.client_db)
    if identity is None:
        raise ReplayError("client spool has no machine/account identity")
    if args.machine != identity["machine"] or args.account != identity["account"]:
        raise ReplayError("replay identity does not match the client spool")
    context = IngestContext(
        machine=identity["machine"],
        account=identity["account"],
        kind=args.kind,
    )
    transport = LocalCenterTransport(
        args.center_db,
        context,
        max_rows=args.max_rows,
        max_bytes=args.max_bytes,
    )
    result = replay_pending(
        args.client_db,
        transport,
        max_rows=args.max_rows,
        max_bytes=args.max_bytes,
    )
    if args.format == "json":
        _print_json(result)
    else:
        print(
            f"replayed {result['sent_facts']} facts in {result['batches']} batches; "
            f"deleted {result['deleted_pending']} pending rows, "
            f"receiver duplicates={result['duplicates']}"
        )
    return 0


def _cmd_receive(args: argparse.Namespace) -> int:
    context = IngestContext(machine=args.machine, account=args.account, kind=args.kind)
    raw = sys.stdin.buffer.read(args.max_bytes + 1)
    ack = receive(
        args.center_db,
        context,
        raw,
        max_rows=args.max_rows,
        max_bytes=args.max_bytes,
    )
    if args.format == "json":
        _print_json(ack)
    else:
        print(
            f"accepted={ack['accepted']} stored={ack['stored']} "
            f"duplicates={ack['duplicates']} conflicts={ack['conflicts']}"
        )
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    report = build_report(args.center_db, month=args.month, tz_name=args.tz)
    if args.format == "json":
        _print_json(report)
    else:
        print(render_report_text(report))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    status = check_status(
        args.center_db,
        expected=_parse_expected(args.expected),
        max_age_seconds=args.max_age_seconds,
    )
    if args.format == "json":
        _print_json(status)
    else:
        print(render_status_text(status))
    return 0 if status["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetre",
        description="Cross-machine agy token usage collector and monthly report (stdlib only).",
    )
    parser.add_argument("--version", action="version", version=f"telemetre {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_parser = sub.add_parser("collect", help="scan agy roots into the local spool")
    collect_parser.add_argument("--client-db", required=True)
    collect_parser.add_argument("--machine", required=True)
    collect_parser.add_argument("--account", required=True)
    collect_parser.add_argument("--root", action="append", required=True)
    collect_parser.add_argument("--collector-version", default=__version__)
    _add_format(collect_parser)
    collect_parser.set_defaults(handler=_cmd_collect)

    pending_parser = sub.add_parser("pending", help="emit the next bounded spool batch")
    pending_parser.add_argument("--client-db", required=True)
    _add_limits(pending_parser)
    _add_format(pending_parser, default="json")
    pending_parser.set_defaults(handler=_cmd_pending)

    replay_parser = sub.add_parser("replay", help="replay the spool through the local seam")
    replay_parser.add_argument("--client-db", required=True)
    replay_parser.add_argument("--center-db", required=True)
    replay_parser.add_argument("--machine", required=True)
    replay_parser.add_argument("--account", required=True)
    replay_parser.add_argument("--kind", default="agy_usage")
    _add_limits(replay_parser)
    _add_format(replay_parser)
    replay_parser.set_defaults(handler=_cmd_replay)

    receive_parser = sub.add_parser("receive", help="bounded local receive seam (stdin -> centre)")
    receive_parser.add_argument("--center-db", required=True)
    receive_parser.add_argument("--machine", required=True)
    receive_parser.add_argument("--account", required=True)
    receive_parser.add_argument("--kind", default="agy_usage")
    _add_limits(receive_parser)
    _add_format(receive_parser, default="json")
    receive_parser.set_defaults(handler=_cmd_receive)

    report_parser = sub.add_parser("report", help="monthly agy usage report from the centre")
    report_parser.add_argument("--center-db", required=True)
    report_parser.add_argument("--month", required=True, help="YYYY-MM in the selected timezone")
    report_parser.add_argument("--tz", default=DEFAULT_TZ)
    _add_format(report_parser)
    report_parser.set_defaults(handler=_cmd_report)

    status_parser = sub.add_parser("status", help="check expected source scans")
    status_parser.add_argument("--center-db", required=True)
    status_parser.add_argument("--expected", action="append", default=[], metavar="MACHINE:ACCOUNT")
    status_parser.add_argument("--max-age-seconds", type=int)
    _add_format(status_parser)
    status_parser.set_defaults(handler=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ProtocolError as exc:
        if args.command == "receive":
            # Never echo validation internals or paths back over the transport.
            print(json.dumps({"ok": False, "error": "invalid batch"}, ensure_ascii=False))
        else:
            print(f"telemetre: protocol error: {exc}", file=sys.stderr)
        return 2
    except (CollectorError, ReplayError, ValueError, sqlite3.Error, OSError) as exc:
        if args.command == "receive":
            print(json.dumps({"ok": False, "error": "receive failed"}, ensure_ascii=False))
        else:
            print(f"telemetre: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
