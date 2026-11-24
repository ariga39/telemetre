import tempfile
import unittest
from pathlib import Path

try:
    from test_quota_support import (
        json_output,
        run_cli,
        source,
        write_config,
        write_fake_agy,
    )
except ImportError:  # running from the repository root
    from tests.test_quota_support import (  # type: ignore
        json_output,
        run_cli,
        source,
        write_config,
        write_fake_agy,
    )


def payload(remaining: float):
    return {
        "status": "success",
        "command": {
            "data": {
                "groups": [
                    {
                        "name": "Gemini",
                        "buckets": [
                            {
                                "id": "weekly",
                                "window": "7d",
                                "remaining_fraction": remaining,
                                "reset_time": "2026-01-08T00:00:00Z",
                            }
                        ],
                    }
                ]
            }
        },
    }


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"
        agy = write_fake_agy(self.tmp, payload=payload(0.5))
        self.config = write_config(
            self.tmp,
            [
                source("agy", "slow", agy, interval_seconds=900),
                source("agy", "fast", agy, interval_seconds=60),
            ],
            database=self.db,
        )

    def test_if_due_uses_each_source_interval_without_provider_activity(self):
        first = run_cli(
            "sample",
            "--config",
            self.config,
            "--if-due",
            "--now",
            "2026-01-01T00:00:00Z",
            "--json",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            [len(item["windows"]) for item in json_output(first)["sampled"]], [1, 1]
        )

        second = run_cli(
            "sample",
            "--config",
            self.config,
            "--if-due",
            "--now",
            "2026-01-01T00:05:00Z",
            "--json",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        outcomes = json_output(second)["sampled"]
        by_account = {item["account"]: item for item in outcomes}
        self.assertTrue(by_account["slow"]["skipped"])
        self.assertFalse(by_account["fast"].get("skipped", False))
        self.assertEqual(by_account["fast"]["status"], "success")

        report = json_output(
            run_cli(
                "report",
                "--config",
                self.config,
                "--now",
                "2026-01-01T00:06:00Z",
                "--json",
            )
        )
        attempts = {
            account["account"]: account["last_attempt"]["sampled_at"]
            for account in report["accounts"]
        }
        self.assertEqual(attempts["slow"], "2026-01-01T00:00:00Z")
        self.assertEqual(attempts["fast"], "2026-01-01T00:05:00Z")

        third = run_cli(
            "sample",
            "--config",
            self.config,
            "--if-due",
            "--now",
            "2026-01-01T00:05:30Z",
            "--json",
        )
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertTrue(all(item.get("skipped") for item in json_output(third)["sampled"]))


if __name__ == "__main__":
    unittest.main()
