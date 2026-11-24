"""Month selection uses event time and the report timezone's calendar month."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support import cli_json, model_step, run_cli, write_transcript


class TimezoneMonthTest(unittest.TestCase):
    def test_utc_and_tokyo_months_select_different_event_sets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            write_transcript(
                root,
                "conv-jan-utc",
                [
                    model_step(
                        0,
                        "2026-01-31T23:30:00Z",
                        input_tokens=1,
                        cache_read_tokens=0,
                        output_tokens=0,
                    )
                ],
            )
            write_transcript(
                root,
                "conv-feb-utc",
                [
                    model_step(
                        0,
                        "2026-02-01T00:30:00Z",
                        input_tokens=2,
                        cache_read_tokens=0,
                        output_tokens=0,
                    )
                ],
            )
            write_transcript(
                root,
                "conv-offset",
                [
                    model_step(
                        0,
                        "2026-02-01T08:00:00+09:00",
                        input_tokens=4,
                        cache_read_tokens=0,
                        output_tokens=0,
                    )
                ],
            )
            client = base / "client.sqlite"
            center = base / "center.sqlite"
            cli_json(
                run_cli(
                    "collect",
                    "--client-db",
                    str(client),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    "--root",
                    str(root),
                    "--format",
                    "json",
                )
            )
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

            january = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-01",
                    "--tz",
                    "UTC",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(january["utc_start"], "2026-01-01T00:00:00Z")
            self.assertEqual(january["utc_end"], "2026-02-01T00:00:00Z")
            self.assertEqual(january["accounts"]["acct"]["account_total"]["input_tokens"], 5)
            self.assertEqual(january["accounts"]["acct"]["account_total"]["model_steps"], 2)

            february_utc = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-02",
                    "--tz",
                    "UTC",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(february_utc["accounts"]["acct"]["account_total"]["input_tokens"], 2)
            self.assertEqual(february_utc["accounts"]["acct"]["account_total"]["model_steps"], 1)

            february_tokyo = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-02",
                    "--tz",
                    "Asia/Tokyo",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(february_tokyo["utc_start"], "2026-01-31T15:00:00Z")
            self.assertEqual(february_tokyo["utc_end"], "2026-02-28T15:00:00Z")
            self.assertEqual(
                february_tokyo["accounts"]["acct"]["account_total"],
                {
                    "input_tokens": 7,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "model_steps": 3,
                },
            )

    def test_invalid_month_and_timezone_fail_at_cli_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            center = Path(tmp) / "center.sqlite"
            bad_month = run_cli(
                "report",
                "--center-db",
                str(center),
                "--month",
                "2026-13",
            )
            self.assertEqual(bad_month.returncode, 2)
            bad_tz = run_cli(
                "report",
                "--center-db",
                str(center),
                "--month",
                "2026-01",
                "--tz",
                "Not/AZone",
            )
            self.assertEqual(bad_tz.returncode, 2)


if __name__ == "__main__":
    unittest.main()
