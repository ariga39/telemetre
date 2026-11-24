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

from telemetre.quota import Observation, WindowObservation, ingest_observation


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


def success_observation(
    sampled_at: str, remaining: float, sample_id: str, machine: str = "test-machine"
):
    return Observation(
        provider="agy",
        account="personal",
        machine=machine,
        sampled_at=sampled_at,
        status="success",
        windows=(
            WindowObservation(
                group="Gemini",
                bucket_id="weekly",
                window="7d",
                window_minutes=10080,
                remaining_value=remaining,
                remaining_unit="fraction",
                remaining_display=remaining * 100.0,
                reset_raw="2026-01-08T00:00:00Z",
                reset_at="2026-01-08T00:00:00Z",
            ),
        ),
        interval_seconds=900,
        sample_id=sample_id,
    )


class OrderingAndClockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"
        self.agy = write_fake_agy(self.tmp, payload=payload(0.5))
        self.config = write_config(
            self.tmp, [source("agy", "personal", self.agy)], database=self.db
        )

    def test_late_replay_of_older_samples_cannot_replace_newer_state(self):
        sampled = run_cli(
            "sample", "--config", self.config, "--now", "2026-01-01T12:00:00Z", "--json"
        )
        self.assertEqual(sampled.returncode, 0, sampled.stderr)
        current = json_output(sampled)["sampled"][0]
        self.assertEqual(current["status"], "success")

        old_failure = Observation(
            provider="agy",
            account="personal",
            machine="test-machine",
            sampled_at="2026-01-01T11:00:00Z",
            status="failure",
            error_kind="timeout",
            error_detail="synthetic old failure",
            interval_seconds=900,
            sample_id="old-failure",
        )
        self.assertTrue(
            ingest_observation(
                self.db, old_failure, received_at="2026-01-01T13:00:00Z"
            )
        )
        old_success = success_observation(
            "2026-01-01T10:00:00Z", 0.1, "old-success"
        )
        self.assertTrue(
            ingest_observation(
                self.db, old_success, received_at="2026-01-01T13:05:00Z"
            )
        )

        report = json_output(
            run_cli(
                "report",
                "--config",
                self.config,
                "--now",
                "2026-01-01T13:06:00Z",
                "--json",
            )
        )
        account = report["accounts"][0]
        self.assertEqual(account["last_attempt"]["sample_id"], current["sample_id"])
        self.assertEqual(account["last_attempt"]["status"], "success")
        self.assertEqual(account["last_success"]["sample_id"], current["sample_id"])
        self.assertEqual(account["windows"][0]["remaining"]["display_percent"], 50.0)

    def test_flags_a_sample_whose_source_clock_is_ahead_of_receipt(self):
        observation = success_observation(
            "2026-01-01T14:10:00Z", 0.3, "future-clock"
        )
        self.assertTrue(
            ingest_observation(
                self.db, observation, received_at="2026-01-01T14:00:00Z"
            )
        )
        report = json_output(
            run_cli(
                "report",
                "--config",
                self.config,
                "--now",
                "2026-01-01T14:11:00Z",
                "--json",
            )
        )
        account = report["accounts"][0]
        self.assertEqual(account["last_attempt"]["sampled_at"], "2026-01-01T14:10:00Z")
        self.assertTrue(account["last_attempt"]["time_anomaly"])
        self.assertTrue(account["windows"][0]["time_anomaly"])
        self.assertEqual(account["windows"][0]["remaining"]["display_percent"], 30.0)

    def test_two_machines_observing_one_account_are_not_summed(self):
        ingest_observation(
            self.db,
            success_observation("2026-01-01T10:00:00Z", 0.9, "machine-a", "alpha"),
            received_at="2026-01-01T10:00:05Z",
        )
        ingest_observation(
            self.db,
            success_observation("2026-01-01T10:05:00Z", 0.4, "machine-b", "beta"),
            received_at="2026-01-01T10:05:05Z",
        )
        report = json_output(
            run_cli(
                "report",
                "--config",
                self.config,
                "--now",
                "2026-01-01T10:06:00Z",
                "--json",
            )
        )
        account = report["accounts"][0]
        self.assertEqual(len(account["windows"]), 1)
        window = account["windows"][0]
        self.assertEqual(window["remaining"]["display_percent"], 40.0)
        self.assertEqual(window["machine"], "beta")
        self.assertEqual(account["last_attempt"]["machine"], "beta")


if __name__ == "__main__":
    unittest.main()
