"""Public CLI surface and forced-command failure behavior."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_delivery_support import (
    delivery_json,
    run_delivery_cli,
    write_bindings,
)


class DeliveryCliTest(unittest.TestCase):
    def test_help_exposes_the_operator_commands(self) -> None:
        process = run_delivery_cli("--help")
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        for command in (
            "send",
            "send-quota",
            "receive",
            "backup",
            "verify-backup",
            "restore",
            "check",
            "run",
        ):
            self.assertIn(command, process.stdout.decode())

    def test_forced_command_refusal_is_json_without_echoing_the_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bindings = write_bindings(
                base / "bindings.json",
                {
                    "alpha-usage": {
                        "machine": "alpha",
                        "account": "acct@example.test",
                        "kind": "agy_usage",
                    }
                },
            )
            secret = b'{"protocol":1,"secret":"SYNTHETIC-RAW-BODY"}'
            result = run_delivery_cli(
                "receive",
                "--bindings",
                str(bindings),
                "--binding-id",
                "alpha-usage",
                "--center-db",
                str(base / "center.sqlite3"),
                stdin=secret,
            )
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertNotIn("SYNTHETIC-RAW-BODY", result.stdout.decode())
            self.assertNotIn("SYNTHETIC-RAW-BODY", result.stderr.decode())

    def test_run_times_out_with_a_distinct_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = run_delivery_cli(
                "run",
                "--lock",
                str(base / "job.lock"),
                "--timeout",
                "0.3",
                "--format",
                "json",
                "--",
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            )
            self.assertEqual(result.returncode, 124)
            payload = delivery_json(result) if result.returncode == 0 else json.loads(result.stdout)
            self.assertTrue(payload["ran"])
            self.assertTrue(payload["timed_out"])


if __name__ == "__main__":
    unittest.main()
