"""Operator CLI for restricted SSH delivery, backup/restore and health.

Use ``python3 -m telemetre.delivery --help``.  Collection, reporting and quota
sampling remain in their own slice CLIs; this command only moves spooled data,
verifies recovery points and checks unattended jobs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .delivery_backup import BackupError, online_backup, restore_backup, verify_backup
from .delivery_bindings import (
    BindingError,
    KIND_AGY_USAGE,
    KIND_QUOTA_SAMPLE,
    load_bindings,
    require_binding,
)
from .delivery_health import check_health, render_text as render_health_text
from .delivery_quota import (
    DEFAULT_QUOTA_MAX_BYTES,
    DeliveryQuotaError,
    replay_pending as replay_quota_pending,
)
from .delivery_receive import (
    DeliveryReceiveError,
    public_error,
    read_bounded,
    receive_bytes,
)
from .delivery_schedule import ScheduleError, run_locked_job
from .delivery_ssh import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MAX_ACK_BYTES,
    DEFAULT_TIMEOUT,
    DeliveryTransportError,
    SSHConfig,
    SSHTransport,
)
from .replay import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, ReplayError, replay_pending
from .receiver import ProtocolError


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _add_format(parser: argparse.ArgumentParser, *, default: str = "text") -> None:
    parser.add_argument("--format", choices=("text", "json"), default=default)


def _add_ssh_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-user", required=True)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--identity", required=True, help="private key file (mode 0600)")
    parser.add_argument("--known-hosts", required=True, help="pinned host-key file")
    parser.add_argument("--host-key-alias")
    parser.add_argument("--ssh-binary", default="ssh")
    parser.add_argument("--connect-timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-ack-bytes", type=int, default=DEFAULT_MAX_ACK_BYTES)
    parser.add_argument(
        "--remote-command",
        default="telemetre-receive",
        help="fixed forced-command token; the host ignores it when configured",
    )


def _transport(binding, args: argparse.Namespace) -> SSHTransport:
    config = SSHConfig(
        host=args.ssh_host,
        user=args.ssh_user,
        identity_file=args.identity,
        known_hosts=args.known_hosts,
        port=args.ssh_port,
        host_key_alias=args.host_key_alias,
        connect_timeout=args.connect_timeout,
        timeout=args.timeout,
        max_batch_bytes=args.max_bytes,
        max_ack_bytes=args.max_ack_bytes,
        ssh_binary=args.ssh_binary,
        remote_command=(args.remote_command,),
    )
    return SSHTransport(binding, config)


def _cmd_send(args: argparse.Namespace) -> int:
    bindings = load_bindings(args.bindings)
    binding = require_binding(bindings, args.binding_id)
    if binding.kind != KIND_AGY_USAGE:
        raise DeliveryReceiveError("send requires an agy_usage binding; use send-quota")
    result = replay_pending(
        args.client_db,
        _transport(binding, args),
        max_rows=args.max_rows,
        max_bytes=args.max_bytes,
    )
    result["binding_id"] = binding.binding_id
    result["kind"] = binding.kind
    if args.format == "json":
        _print_json(result)
    else:
        print(
            f"delivered {result['sent_facts']} facts in {result['batches']} batches; "
            f"deleted {result['deleted_pending']} pending rows, "
            f"receiver duplicates={result['duplicates']}"
        )
    return 0


def _cmd_send_quota(args: argparse.Namespace) -> int:
    bindings = load_bindings(args.bindings)
    binding = require_binding(bindings, args.binding_id)
    if binding.kind != KIND_QUOTA_SAMPLE:
        raise DeliveryReceiveError("send-quota requires a quota_sample binding")
    result = replay_quota_pending(
        args.quota_db,
        _transport(binding, args),
        binding=binding,
        max_bytes=args.max_bytes,
    )
    result["binding_id"] = binding.binding_id
    result["kind"] = binding.kind
    result["provider"] = binding.provider
    if args.format == "json":
        _print_json(result)
    else:
        print(
            f"delivered {result['sent_observations']} quota observations in "
            f"{result['batches']} batches; duplicates={result['duplicates']}"
        )
    return 0


def _cmd_receive(args: argparse.Namespace) -> int:
    bindings = load_bindings(args.bindings)
    raw = read_bounded(sys.stdin.buffer, args.max_bytes)
    ack = receive_bytes(
        args.center_db,
        bindings,
        args.binding_id,
        raw,
        max_rows=args.max_rows,
        max_bytes=args.max_bytes,
    )
    _print_json(ack)
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    result = online_backup(args.center_db, args.dest, prefix=args.prefix)
    if args.format == "json":
        _print_json(result)
    else:
        print(f"backup {result['path']} ({result['bytes']} bytes, integrity {result['integrity']})")
    return 0


def _cmd_verify_backup(args: argparse.Namespace) -> int:
    result = verify_backup(args.backup)
    if args.format == "json":
        _print_json(result)
    else:
        print(f"backup {result['path']} is intact ({result['bytes']} bytes)")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    result = restore_backup(args.backup, args.center_db)
    if args.format == "json":
        _print_json(result)
    else:
        print(f"restored {result['target']} from {result['backup']}")
    return 0


def _parse_usage(value: str) -> tuple[str, str]:
    machine, separator, account = value.partition(":")
    if not separator or not machine or not account:
        raise ValueError("--expect-usage must be machine:account")
    return machine, account


def _parse_quota(value: str) -> tuple[str, str, str]:
    parts = value.split(":")
    if len(parts) != 3 or not all(parts):
        raise ValueError("--expect-quota must be machine:provider:account")
    return parts[0], parts[1], parts[2]


def _cmd_check(args: argparse.Namespace) -> int:
    result = check_health(
        args.center_db,
        expected_usage=[_parse_usage(item) for item in args.expect_usage],
        expected_quota=[_parse_quota(item) for item in args.expect_quota],
        max_age_seconds=args.max_age_seconds,
    )
    if args.format == "json":
        _print_json(result)
    else:
        print(render_health_text(result))
    return 0 if result["ok"] else 3


def _cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    result = run_locked_job(
        command,
        lock_path=args.lock,
        timeout=args.timeout,
    )
    if args.format == "json":
        _print_json(result)
    else:
        if not result["ran"]:
            print(f"skipped: {result['reason']} ({result['lock_path']})")
        else:
            print(
                f"{result['reason']}: exit={result['exit_code']} "
                f"duration={result['duration_seconds']}s"
            )
            if result.get("error"):
                print(result["error"], file=sys.stderr)
            if result.get("stdout"):
                sys.stdout.write(result["stdout"])
            if result.get("stderr"):
                sys.stderr.write(result["stderr"])
    if not result["ran"]:
        return 0
    if result["timed_out"]:
        return 124
    return int(result["exit_code"] or 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m telemetre.delivery",
        description=(
            "Restricted SSH delivery, online SQLite backup/restore, scheduling "
            "and expected-source health checks for telemetre."
        ),
    )
    parser.add_argument("--version", action="version", version="telemetre.delivery 0.1")
    sub = parser.add_subparsers(dest="command", required=True)

    send = sub.add_parser("send", help="send pending agy_usage facts over SSH")
    send.add_argument("--client-db", required=True)
    send.add_argument("--bindings", required=True)
    send.add_argument("--binding-id", required=True)
    send.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    send.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    _add_ssh_options(send)
    _add_format(send)
    send.set_defaults(handler=_cmd_send)

    send_quota = sub.add_parser("send-quota", help="send pending quota observations over SSH")
    send_quota.add_argument("--quota-db", required=True)
    send_quota.add_argument("--bindings", required=True)
    send_quota.add_argument("--binding-id", required=True)
    send_quota.add_argument("--max-bytes", type=int, default=DEFAULT_QUOTA_MAX_BYTES)
    _add_ssh_options(send_quota)
    _add_format(send_quota)
    send_quota.set_defaults(handler=_cmd_send_quota)

    receive = sub.add_parser("receive", help="forced-command receiver (stdin -> centre)")
    receive.add_argument("--bindings", required=True)
    receive.add_argument("--binding-id", required=True)
    receive.add_argument("--center-db", required=True)
    receive.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    receive.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    receive.set_defaults(handler=_cmd_receive)

    backup = sub.add_parser("backup", help="take a verified online centre snapshot")
    backup.add_argument("--center-db", required=True)
    backup.add_argument("--dest", required=True)
    backup.add_argument("--prefix", default="telemetre-center")
    _add_format(backup)
    backup.set_defaults(handler=_cmd_backup)

    verify = sub.add_parser("verify-backup", help="verify a snapshot read-only")
    verify.add_argument("--backup", required=True)
    _add_format(verify)
    verify.set_defaults(handler=_cmd_verify_backup)

    restore = sub.add_parser("restore", help="restore a verified snapshot to a new path")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--center-db", required=True)
    _add_format(restore)
    restore.set_defaults(handler=_cmd_restore)

    check = sub.add_parser("check", help="check expected usage and quota sources")
    check.add_argument("--center-db", required=True)
    check.add_argument("--expect-usage", action="append", default=[], metavar="MACHINE:ACCOUNT")
    check.add_argument(
        "--expect-quota",
        action="append",
        default=[],
        metavar="MACHINE:PROVIDER:ACCOUNT",
    )
    check.add_argument("--max-age-seconds", type=int)
    _add_format(check)
    check.set_defaults(handler=_cmd_check)

    run = sub.add_parser("run", help="run one command under a single-instance lock")
    run.add_argument("--lock", required=True)
    run.add_argument("--timeout", type=float, required=True)
    _add_format(run)
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(handler=_cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (BackupError, BindingError, DeliveryQuotaError, DeliveryReceiveError,
            DeliveryTransportError, ProtocolError, ReplayError, ScheduleError,
            ValueError, sqlite3.Error, OSError) as exc:
        if args.command == "receive":
            _print_json({"ok": False, "error": public_error(exc)})
        elif getattr(args, "format", "text") == "json":
            _print_json({"ok": False, "error": " ".join(str(exc).split())[:500]})
        else:
            print(f"telemetre.delivery: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
