"""Spool durability, partial lines, bad-line diagnostics and status-only runs."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.support import (
    append_raw,
    cli_json,
    model_step,
    run_cli,
    write_transcript,
)


def _collect(base: Path, root: Path, client: Path, *, machine: str = "alpha") -> dict:
    return cli_json(
        run_cli(
            "collect",
            "--client-db",
            str(client),
            "--machine",
            machine,
            "--account",
            "acct",
            "--root",
            str(root),
            "--format",
            "json",
        )
    )


def _replay(base: Path, client: Path, center: Path, *, machine: str = "alpha") -> dict:
    return cli_json(
        run_cli(
            "replay",
            "--client-db",
            str(client),
            "--center-db",
            str(center),
            "--machine",
            machine,
            "--account",
            "acct",
            "--format",
            "json",
        )
    )


def _report(center: Path, month: str = "2026-11", tz: str = "UTC") -> dict:
    return cli_json(
        run_cli(
            "report",
            "--center-db",
            str(center),
            "--month",
            month,
            "--tz",
            tz,
            "--format",
            "json",
        )
    )


class SpoolDiagnosticsTest(unittest.TestCase):
    def test_partial_tail_waits_for_newline_and_unchanged_file_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            path = write_transcript(
                root,
                "conv-partial",
                [
                    model_step(
                        0,
                        "2026-11-04T00:00:00Z",
                        input_tokens=10,
                        cache_read_tokens=1,
                        output_tokens=2,
                    )
                ],
            )
            second = json.dumps(
                model_step(
                    1,
                    "2026-11-04T00:01:00Z",
                    input_tokens=20,
                    cache_read_tokens=2,
                    output_tokens=3,
                ),
                separators=(",", ":"),
            )
            append_raw(path, second[: len(second) // 2])

            first = _collect(base, root, client)
            self.assertEqual(first["new_facts"], 1)
            self.assertEqual(first["partial_files"], 1)
            self.assertEqual(first["bad_lines"], 0)

            # The half line has not been completed yet; a second run must not
            # turn it into a bad line or a fact.
            still_partial = _collect(base, root, client)
            self.assertEqual(still_partial["new_facts"], 0)
            self.assertEqual(still_partial["partial_files"], 1)
            self.assertEqual(still_partial["bad_lines"], 0)

            append_raw(path, second[len(second) // 2 :] + "\n")
            completed = _collect(base, root, client)
            self.assertEqual(completed["new_facts"], 1)
            self.assertEqual(completed["partial_files"], 0)
            self.assertEqual(completed["bad_lines"], 0)
            self.assertEqual(completed["pending_backlog"], 2)

            unchanged = _collect(base, root, client)
            self.assertEqual(unchanged["files_skipped"], 1)
            self.assertEqual(unchanged["files_read"], 0)
            self.assertEqual(unchanged["new_facts"], 0)
            self.assertEqual(unchanged["pending_backlog"], 2)

            _replay(base, client, center)
            account = _report(center)["accounts"]["acct"]
            self.assertEqual(account["account_total"], {
                "input_tokens": 30,
                "cache_read_tokens": 3,
                "output_tokens": 5,
                "model_steps": 2,
            })

    def test_middle_bad_line_is_counted_and_zero_is_not_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            path = write_transcript(
                root,
                "conv-bad",
                [
                    model_step(
                        0,
                        "2026-11-05T00:00:00Z",
                        input_tokens=0,
                        cache_read_tokens=0,
                        output_tokens=0,
                    )
                ],
            )
            append_raw(path, "{not-json}\n")
            append_raw(
                path,
                json.dumps(
                    model_step(
                        1,
                        "2026-11-05T00:01:00Z",
                        include=(),
                    ),
                    separators=(",", ":"),
                )
                + "\n",
            )

            collected = _collect(base, root, client)
            self.assertEqual(collected["bad_lines"], 1)
            self.assertEqual(collected["missing_count_steps"], 1)
            self.assertEqual(collected["new_facts"], 2)

            _replay(base, client, center)
            report = _report(center)
            account = report["accounts"]["acct"]
            self.assertEqual(account["account_total"]["input_tokens"], 0)
            self.assertEqual(account["account_total"]["model_steps"], 2)
            self.assertEqual(account["coverage"]["missing"], {
                "input_tokens": 1,
                "cache_read_tokens": 1,
                "output_tokens": 1,
            })
            self.assertEqual(report["sources"][0]["bad_lines"], 1)

    def test_oversized_local_integer_is_bad_line_and_later_rows_still_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            write_transcript(
                root,
                "conv-overflow",
                [
                    model_step(
                        0,
                        "2026-11-22T00:00:00Z",
                        input_tokens=10,
                        cache_read_tokens=1,
                        output_tokens=2,
                    ),
                    model_step(
                        1,
                        "2026-11-22T00:01:00Z",
                        input_tokens=2**63,
                        cache_read_tokens=1,
                        output_tokens=2,
                    ),
                    model_step(
                        2,
                        "2026-11-22T00:02:00Z",
                        input_tokens=30,
                        cache_read_tokens=3,
                        output_tokens=4,
                    ),
                ],
            )
            collected = _collect(base, root, client)
            self.assertEqual(collected["bad_lines"], 1)
            self.assertEqual(collected["new_facts"], 2)
            _replay(base, client, center)
            account = _report(center)["accounts"]["acct"]
            self.assertEqual(account["account_total"], {
                "input_tokens": 40,
                "cache_read_tokens": 4,
                "output_tokens": 6,
                "model_steps": 2,
            })

    def test_source_deleted_after_collection_and_lost_ack_do_not_lose_or_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            write_transcript(
                root,
                "conv-deleted",
                [
                    model_step(
                        0,
                        "2026-11-06T00:00:00Z",
                        input_tokens=7,
                        cache_read_tokens=3,
                        output_tokens=4,
                    )
                ],
            )
            _collect(base, root, client)
            batch = run_cli("pending", "--client-db", str(client))
            self.assertEqual(batch.returncode, 0, batch.stderr.decode())

            # Source is gone before delivery; the staged fact must survive.
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                else:
                    path.rmdir()
            root.rmdir()

            # Deliver, then replay the exact same batch as if the ack was lost.
            first = cli_json(
                run_cli(
                    "receive",
                    "--center-db",
                    str(center),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    stdin=batch.stdout,
                )
            )
            self.assertEqual(first["stored"], 1)
            second = cli_json(
                run_cli(
                    "receive",
                    "--center-db",
                    str(center),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    stdin=batch.stdout,
                )
            )
            self.assertEqual(second["stored"], 0)
            self.assertEqual(second["duplicates"], 1)

            report = _report(center)
            self.assertEqual(report["accounts"]["acct"]["account_total"], {
                "input_tokens": 7,
                "cache_read_tokens": 3,
                "output_tokens": 4,
                "model_steps": 1,
            })

            replayed = _replay(base, client, center)
            self.assertEqual(replayed["deleted_pending"], 1)

    def test_older_scan_does_not_regress_machine_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            path = write_transcript(
                root,
                "conv-order",
                [
                    model_step(
                        0,
                        "2026-11-14T00:00:00Z",
                        input_tokens=1,
                        cache_read_tokens=0,
                        output_tokens=0,
                    )
                ],
            )
            _collect(base, root, client)
            older = json.loads(run_cli("pending", "--client-db", str(client)).stdout)

            append_raw(
                path,
                json.dumps(
                    model_step(
                        1,
                        "2026-11-14T00:01:00Z",
                        input_tokens=2,
                        cache_read_tokens=0,
                        output_tokens=0,
                    ),
                    separators=(",", ":"),
                )
                + "\n",
            )
            _collect(base, root, client)
            newer = json.loads(run_cli("pending", "--client-db", str(client)).stdout)
            self.assertNotEqual(older["scan"]["completed_at"], newer["scan"]["completed_at"])

            # Newer batch arrives first; the delayed older batch must not pull
            # the last-successful-scan timestamp backwards.
            for batch in (newer, older):
                cli_json(
                    run_cli(
                        "receive",
                        "--center-db",
                        str(center),
                        "--machine",
                        "alpha",
                        "--account",
                        "acct",
                        stdin=json.dumps(batch).encode(),
                    )
                )
            status = cli_json(
                run_cli(
                    "status",
                    "--center-db",
                    str(center),
                    "--expected",
                    "alpha:acct",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(
                status["checks"][0]["last_success_at"],
                newer["scan"]["completed_at"],
            )
            report = _report(center)
            self.assertEqual(
                report["sources"][0]["last_success_at"],
                newer["scan"]["completed_at"],
            )

    def test_source_read_error_is_exposed_in_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            broken = (
                root
                / "brain"
                / "conv-unreadable"
                / ".system_generated"
                / "logs"
                / "transcript.jsonl"
            )
            broken.mkdir(parents=True)
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            collected = _collect(base, root, client)
            self.assertEqual(collected["files_seen"], 1)
            self.assertEqual(collected["source_errors"], 1)
            _replay(base, client, center)
            report = _report(center)
            self.assertEqual(report["sources"][0]["source_errors"], 1)
            self.assertEqual(report["sources"][0]["files_seen"], 1)
            self.assertEqual(report["sources"][0]["files_read"], 0)
            status = run_cli(
                "status",
                "--center-db",
                str(center),
                "--expected",
                "alpha:acct",
                "--format",
                "json",
            )
            self.assertEqual(status.returncode, 1)
            check = json.loads(status.stdout)["checks"][0]
            self.assertFalse(check["ok"])
            self.assertEqual(check["source_errors"], 1)
            self.assertEqual(check["files_seen"], 1)
            self.assertEqual(check["files_read"], 0)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "permissions do not block root")
    def test_unreadable_directory_is_observed_and_fails_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            write_transcript(
                root,
                "conv-hidden",
                [
                    model_step(
                        0,
                        "2026-11-23T00:00:00Z",
                        input_tokens=1,
                        cache_read_tokens=2,
                        output_tokens=3,
                    )
                ],
            )
            locked = root / "brain" / "conv-hidden"
            locked.chmod(0o000)
            try:
                client = base / "client.sqlite"
                center = base / "center.sqlite"
                collected = _collect(base, root, client)
                self.assertGreaterEqual(collected["source_errors"], 1)
                self.assertEqual(collected["files_seen"], 0)
                _replay(base, client, center)
                source = _report(center)["sources"][0]
                self.assertGreaterEqual(source["source_errors"], 1)
                self.assertEqual(source["files_seen"], 0)
                self.assertEqual(source["files_read"], 0)
                status = run_cli(
                    "status",
                    "--center-db",
                    str(center),
                    "--expected",
                    "alpha:acct",
                    "--format",
                    "json",
                )
                self.assertEqual(status.returncode, 1)
                check = json.loads(status.stdout)["checks"][0]
                self.assertFalse(check["ok"])
                self.assertEqual(check["reason"], "source read/traversal errors")
            finally:
                locked.chmod(0o755)

    def test_healthy_zero_use_scan_is_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            (root / "brain").mkdir(parents=True)
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            collected = _collect(base, root, client)
            self.assertEqual(collected["files_seen"], 0)
            self.assertEqual(collected["files_read"], 0)
            self.assertEqual(collected["source_errors"], 0)
            _replay(base, client, center)
            source = _report(center)["sources"][0]
            self.assertEqual(source["files_seen"], 0)
            self.assertEqual(source["files_read"], 0)
            self.assertEqual(source["source_errors"], 0)
            status = run_cli(
                "status",
                "--center-db",
                str(center),
                "--expected",
                "alpha:acct",
                "--format",
                "json",
            )
            self.assertEqual(status.returncode, 0, status.stdout.decode())
            self.assertTrue(json.loads(status.stdout)["ok"])

    def test_status_only_run_is_reported_and_expected_source_gate_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            write_transcript(
                root,
                "conv-status",
                [
                    model_step(
                        0,
                        "2026-11-07T00:00:00Z",
                        input_tokens=1,
                        cache_read_tokens=2,
                        output_tokens=3,
                    )
                ],
            )
            _collect(base, root, client)
            _replay(base, client, center)

            missing = run_cli(
                "status",
                "--center-db",
                str(center),
                "--expected",
                "alpha:acct",
                "--format",
                "json",
            )
            self.assertEqual(missing.returncode, 0, missing.stdout.decode())
            self.assertTrue(json.loads(missing.stdout)["ok"])

            # No new facts, but a new scan run must still refresh centre status.
            _collect(base, root, client)
            _replay(base, client, center)
            status = cli_json(
                run_cli(
                    "status",
                    "--center-db",
                    str(center),
                    "--expected",
                    "alpha:acct",
                    "--format",
                    "json",
                )
            )
            self.assertTrue(status["ok"])
            self.assertEqual(status["checks"][0]["bad_lines"], 0)
            self.assertEqual(status["checks"][0]["files_seen"], 1)
            self.assertEqual(status["checks"][0]["files_read"], 0)
            self.assertEqual(status["checks"][0]["source_errors"], 0)

            absent = run_cli(
                "status",
                "--center-db",
                str(center),
                "--expected",
                "beta:acct",
                "--format",
                "json",
            )
            self.assertEqual(absent.returncode, 1)
            self.assertFalse(json.loads(absent.stdout)["ok"])

    def test_missing_source_is_a_failed_expected_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            collected = _collect(base, base / "does-not-exist", client)
            self.assertTrue(collected["source_missing"])
            _replay(base, client, center)
            status = run_cli(
                "status",
                "--center-db",
                str(center),
                "--expected",
                "alpha:acct",
                "--format",
                "json",
            )
            self.assertEqual(status.returncode, 1)
            self.assertEqual(
                json.loads(status.stdout)["checks"][0]["reason"],
                "expected source missing",
            )


if __name__ == "__main__":
    unittest.main()
