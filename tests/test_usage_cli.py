"""Vertical slice 1: collect two machines, replay, report the UTC month."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from telemetre.cli import main
from tests.support import cli_json, model_step, run_cli, write_transcript


class TwoMachineMonthlyReportTest(unittest.TestCase):
    def test_invalid_utf8_root_is_reported_without_losing_good_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            good_root = base / "good"
            write_transcript(
                good_root,
                "conv-good",
                [
                    model_step(
                        0,
                        "2026-11-27T00:00:00Z",
                        input_tokens=10,
                        cache_read_tokens=1,
                        output_tokens=2,
                    )
                ],
            )
            bad_root = os.fsdecode(os.fsencode(str(base)) + b"/bad-\xff")
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            buffer = io.BytesIO()
            stream = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict")
            with contextlib.redirect_stdout(stream):
                rc = main(
                    [
                        "collect",
                        "--client-db",
                        str(client),
                        "--machine",
                        "alpha",
                        "--account",
                        "acct",
                        "--root",
                        bad_root,
                        "--root",
                        str(good_root),
                        "--format",
                        "json",
                    ]
                )
            stream.flush()
            self.assertEqual(rc, 0)
            collected = json.loads(buffer.getvalue().decode("utf-8"))
            self.assertEqual(collected["new_facts"], 1)
            self.assertGreaterEqual(collected["source_errors"], 1)
            self.assertEqual(collected["invalid_roots"], 1)
            self.assertFalse(collected["source_missing"])
            self.assertEqual(collected["roots"], [str(good_root)])

            cli_json(
                run_cli(
                    "replay",
                    "--client-db",
                    str(client),
                    "--center-db",
                    str(center),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    "--format",
                    "json",
                )
            )
            report = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-11",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(
                report["accounts"]["acct"]["account_total"],
                {
                    "input_tokens": 10,
                    "cache_read_tokens": 1,
                    "output_tokens": 2,
                    "model_steps": 1,
                },
            )

    def test_two_roots_on_one_machine_are_both_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root_a = base / "root-a"
            root_b = base / "root-b"
            write_transcript(
                root_a,
                "conv-root-a",
                [
                    model_step(
                        0,
                        "2026-11-20T00:00:00Z",
                        input_tokens=5,
                        cache_read_tokens=1,
                        output_tokens=2,
                    )
                ],
            )
            write_transcript(
                root_b,
                "conv-root-b",
                [
                    model_step(
                        0,
                        "2026-11-21T00:00:00Z",
                        input_tokens=7,
                        cache_read_tokens=2,
                        output_tokens=3,
                    )
                ],
            )
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            collected = cli_json(
                run_cli(
                    "collect",
                    "--client-db",
                    str(client),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    "--root",
                    str(root_a),
                    "--root",
                    str(root_b),
                    "--format",
                    "json",
                )
            )
            self.assertEqual(collected["new_facts"], 2)
            cli_json(
                run_cli(
                    "replay",
                    "--client-db",
                    str(client),
                    "--center-db",
                    str(center),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    "--format",
                    "json",
                )
            )
            report = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-11",
                    "--format",
                    "json",
                )
            )
            account = report["accounts"]["acct"]
            self.assertEqual(account["account_total"], {
                "input_tokens": 12,
                "cache_read_tokens": 3,
                "output_tokens": 5,
                "model_steps": 2,
            })
            self.assertEqual(len(account["machines"]), 1)
            self.assertEqual(account["machines"][0]["machine"], "alpha")

    def test_two_roots_report_one_utc_month_and_account_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root_a = base / "a"
            root_b = base / "b"

            write_transcript(
                root_a,
                "conv-a",
                [
                    model_step(
                        0,
                        "2026-10-31T23:30:00Z",
                        input_tokens=100,
                        cache_read_tokens=10,
                        output_tokens=5,
                    ),
                    model_step(
                        1,
                        "2026-11-01T00:30:00Z",
                        input_tokens=200,
                        cache_read_tokens=20,
                        output_tokens=6,
                    ),
                    {
                        "type": "TOOL_CALL",
                        "status": "DONE",
                        "step_index": 2,
                        "model": "gemini-3-pro",
                        "input_tokens": 999,
                        "cache_read_tokens": 999,
                        "output_tokens": 999,
                    },
                ],
            )
            write_transcript(
                root_b,
                "conv-b",
                [
                    model_step(
                        0,
                        "2026-11-15T12:00:00Z",
                        model="gemini-3-flash",
                        input_tokens=400,
                        cache_read_tokens=40,
                        output_tokens=7,
                    )
                ],
            )

            client_a = base / "client-a.sqlite"
            client_b = base / "client-b.sqlite"
            center = base / "center.sqlite"

            collect_a = run_cli(
                "collect",
                "--client-db",
                str(client_a),
                "--machine",
                "alpha",
                "--account",
                "acct@example.test",
                "--root",
                str(root_a),
                "--format",
                "json",
            )
            cli_json(collect_a)
            collect_b = run_cli(
                "collect",
                "--client-db",
                str(client_b),
                "--machine",
                "beta",
                "--account",
                "acct@example.test",
                "--root",
                str(root_b),
                "--format",
                "json",
            )
            cli_json(collect_b)

            cli_json(
                run_cli(
                    "replay",
                    "--client-db",
                    str(client_a),
                    "--center-db",
                    str(center),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct@example.test",
                    "--format",
                    "json",
                )
            )
            cli_json(
                run_cli(
                    "replay",
                    "--client-db",
                    str(client_b),
                    "--center-db",
                    str(center),
                    "--machine",
                    "beta",
                    "--account",
                    "acct@example.test",
                    "--format",
                    "json",
                )
            )

            report = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-11",
                    "--tz",
                    "UTC",
                    "--format",
                    "json",
                )
            )

            self.assertEqual(report["month"], "2026-11")
            self.assertEqual(report["timezone"], "UTC")
            self.assertEqual(report["utc_start"], "2026-11-01T00:00:00Z")
            self.assertEqual(report["utc_end"], "2026-12-01T00:00:00Z")

            account = report["accounts"]["acct@example.test"]
            self.assertEqual(account["account_total"], {
                "input_tokens": 600,
                "cache_read_tokens": 60,
                "output_tokens": 13,
                "model_steps": 2,
            })
            self.assertEqual(account["machines"], [
                {
                    "machine": "alpha",
                    "models": [{
                        "model": "gemini-3-pro",
                        "input_tokens": 200,
                        "cache_read_tokens": 20,
                        "output_tokens": 6,
                        "model_steps": 1,
                    }],
                },
                {
                    "machine": "beta",
                    "models": [{
                        "model": "gemini-3-flash",
                        "input_tokens": 400,
                        "cache_read_tokens": 40,
                        "output_tokens": 7,
                        "model_steps": 1,
                    }],
                },
            ])
            self.assertEqual(account["unattributed"], [])
            self.assertEqual(account["conflicts"], [])
            self.assertEqual(account["coverage"]["steps"], 2)
            self.assertEqual(account["coverage"]["reasoning"], "unknown")


if __name__ == "__main__":
    unittest.main()
