import tempfile
import unittest
from datetime import datetime, timedelta
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

from telemetre.quota import (
    Observation,
    WindowObservation,
    get_daily_summary,
    ingest_observation,
)


def payload(remaining: float, reset: str):
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
                                "reset_time": reset,
                            }
                        ],
                    }
                ]
            }
        },
    }


R1 = "2026-01-08T00:00:00Z"
R2 = "2026-01-15T00:00:00Z"


class DatedReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"
        self.agy = write_fake_agy(self.tmp, payload=payload(0.9, R1))
        self.config = write_config(
            self.tmp,
            [source("agy", "personal", self.agy, interval_seconds=900)],
            database=self.db,
            timezone="Asia/Shanghai",
        )
        self.samples = [
            ("2025-12-31T15:30:00Z", 0.9, R1),
            ("2025-12-31T16:10:00Z", 0.6, R1),
            ("2026-01-01T02:00:00Z", 0.0, R1),
            ("2026-01-01T02:15:00Z", 0.0, R1),
            ("2026-01-01T03:30:00Z", 0.2, R1),
            ("2026-01-01T03:45:00Z", 0.0, R1),
            ("2026-01-01T03:50:00Z", 0.0, R1),
            ("2026-01-01T04:00:00Z", 0.8, R2),
        ]
        for when, remaining, reset in self.samples:
            write_fake_agy(self.tmp, payload=payload(remaining, reset))
            process = run_cli(
                "sample", "--config", self.config, "--now", when, "--json"
            )
            self.assertEqual(process.returncode, 0, process.stderr)

    def date_report(self):
        process = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-01T05:00:00Z",
            "--json",
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return json_output(process)

    def test_date_report_uses_the_selected_timezone_and_observes_bottom_segments(self):
        report = self.date_report()
        self.assertEqual(report["mode"], "date")
        self.assertEqual(report["date"], "2026-01-01")
        self.assertEqual(report["timezone"], "Asia/Shanghai")
        self.assertEqual(report["range"]["start"], "2025-12-31T16:00:00Z")
        self.assertEqual(report["range"]["end"], "2026-01-01T05:00:00Z")
        self.assertFalse(report["range"]["complete"])

        account = report["accounts"][0]
        self.assertEqual(account["last_attempt"]["sampled_at"], "2026-01-01T04:00:00Z")
        self.assertEqual(account["last_success_in_range"], "2026-01-01T04:00:00Z")
        self.assertTrue(account["missing"])
        self.assertGreaterEqual(len(account["missing_gaps"]), 1)
        self.assertEqual(len(account["windows"]), 2)

        by_reset = {window["reset"]["at"]: window for window in account["windows"]}
        first = by_reset[R1]
        self.assertEqual(first["minimum"]["display_percent"], 0.0)
        self.assertEqual(first["minimum"]["sampled_at"], "2026-01-01T02:00:00Z")
        self.assertEqual(first["latest"]["display_percent"], 0.0)
        self.assertEqual(first["latest"]["sampled_at"], "2026-01-01T03:50:00Z")
        self.assertEqual(first["bottom_segments"], 2)
        self.assertEqual(first["observations"], 6)
        self.assertTrue(first["has_gap"])

        second = by_reset[R2]
        self.assertEqual(second["minimum"]["display_percent"], 80.0)
        self.assertEqual(second["bottom_segments"], 0)
        self.assertEqual(second["observations"], 1)

        # The sample from Dec 31 local time is outside the requested day.
        all_mins = [window["minimum"]["display_percent"] for window in account["windows"]]
        self.assertNotIn(90.0, all_mins)

    def test_daily_summary_is_stored_once_and_can_be_read_back(self):
        first = run_cli(
            "daily",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-01T05:00:00Z",
            "--json",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        summary = json_output(first)
        self.assertEqual(summary["mode"], "daily")
        self.assertEqual(summary["date"], "2026-01-01")
        self.assertTrue(summary["stored"])

        second = run_cli(
            "daily",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-01T05:05:00Z",
            "--json",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        stored = get_daily_summary(self.db, "2026-01-01", "Asia/Shanghai")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["date"], "2026-01-01")
        self.assertEqual(stored["mode"], "daily")
        self.assertEqual(stored["timezone"], "Asia/Shanghai")
        self.assertEqual(len(stored["accounts"][0]["windows"]), 2)

        viewed = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-01T05:06:00Z",
            "--json",
        )
        self.assertEqual(viewed.returncode, 0, viewed.stderr)
        self.assertEqual(json_output(viewed)["date"], "2026-01-01")


class MixedCadenceHistoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"
        self.agy = write_fake_agy(self.tmp, payload=payload(0.5, R1))
        self.config = write_config(
            self.tmp,
            [source("agy", "personal", self.agy, interval_seconds=900)],
            database=self.db,
            timezone="UTC",
        )
        self.counter = 0

    def add(self, when: datetime, interval_seconds: int) -> None:
        sampled_at = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        observation = Observation(
            provider="agy",
            account="personal",
            machine="test-machine",
            sampled_at=sampled_at,
            status="success",
            windows=(
                WindowObservation(
                    group="Gemini",
                    bucket_id="weekly",
                    window="7d",
                    window_minutes=10080,
                    remaining_value=0.5,
                    remaining_unit="fraction",
                    remaining_display=50.0,
                    reset_raw=R1,
                    reset_at=R1,
                ),
            ),
            interval_seconds=interval_seconds,
            sample_id=f"mixed-{self.counter}",
        )
        self.counter += 1
        ingest_observation(self.db, observation, received_at=when)

    def report(self):
        process = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-02T00:10:00Z",
            "--json",
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return json_output(process)

    def test_actual_missed_fast_segment_survives_slow_cadence_changes(self):
        day = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        for index in range(24):
            self.add(day + timedelta(minutes=15 * index), 900)
        for minute in (0, 1, 2):
            self.add(day + timedelta(hours=6, minutes=minute), 60)
        self.add(day + timedelta(hours=6, minutes=5), 900)
        for index in range(70):
            self.add(day + timedelta(hours=6, minutes=20 + 15 * index), 900)
        for minute in range(40, 60):
            self.add(day + timedelta(hours=23, minutes=minute), 60)

        report = self.report()
        account = report["accounts"][0]
        self.assertEqual(account["interval_seconds"], 60)
        self.assertFalse(account["stale"])
        self.assertTrue(account["missing"])
        self.assertEqual(len(account["missing_gaps"]), 1)
        self.assertEqual(
            account["missing_gaps"][0]["from"], "2026-01-01T06:02:00Z"
        )
        self.assertEqual(
            account["missing_gaps"][0]["to"], "2026-01-01T06:05:00Z"
        )
        self.assertTrue(account["windows"][0]["has_gap"])

    def test_healthy_slow_segments_with_a_fast_tail_have_no_gaps(self):
        day = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        for index in range(95):
            self.add(day + timedelta(minutes=15 * index), 900)
        for minute in range(35, 60):
            self.add(day + timedelta(hours=23, minutes=minute), 60)

        report = self.report()
        account = report["accounts"][0]
        self.assertEqual(account["interval_seconds"], 60)
        self.assertFalse(account["missing"])
        self.assertEqual(account["missing_gaps"], [])
        self.assertFalse(account["stale"])
        self.assertFalse(account["windows"][0]["has_gap"])

        checked = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-02T00:10:00Z",
            "--check",
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)


class PeriodEndStalenessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"
        self.agy = write_fake_agy(self.tmp, payload=payload(0.5, R1))
        self.config = write_config(
            self.tmp,
            [source("agy", "personal", self.agy, interval_seconds=900)],
            database=self.db,
            timezone="Asia/Shanghai",
        )
        start = datetime.fromisoformat("2025-12-31T16:00:00+00:00")
        for index in range(96):
            when = start + timedelta(minutes=15 * index)
            sampled_at = when.strftime("%Y-%m-%dT%H:%M:%SZ")
            observation = Observation(
                provider="agy",
                account="personal",
                machine="test-machine",
                sampled_at=sampled_at,
                status="success",
                windows=(
                    WindowObservation(
                        group="Gemini",
                        bucket_id="weekly",
                        window="7d",
                        window_minutes=10080,
                        remaining_value=0.5,
                        remaining_unit="fraction",
                        remaining_display=50.0,
                        reset_raw=R1,
                        reset_at=R1,
                    ),
                ),
                interval_seconds=900,
                sample_id=f"period-{index}",
            )
            ingest_observation(self.db, observation, received_at=when)

    def test_history_uses_the_recorded_interval_not_a_changed_current_config(self):
        write_config(
            self.tmp,
            [source("agy", "personal", self.agy, interval_seconds=60)],
            database=self.db,
            timezone="Asia/Shanghai",
        )
        report = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-02T00:10:00Z",
            "--json",
        )
        self.assertEqual(report.returncode, 0, report.stderr)
        account = json_output(report)["accounts"][0]
        self.assertEqual(account["interval_seconds"], 900)
        self.assertFalse(account["missing"])
        self.assertFalse(account["stale"])
        checked = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-02T00:10:00Z",
            "--check",
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_dated_report_and_daily_use_the_requested_period_end_for_staleness(self):
        report = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-02T00:10:00Z",
            "--json",
        )
        self.assertEqual(report.returncode, 0, report.stderr)
        account = json_output(report)["accounts"][0]
        self.assertFalse(account["stale"])
        self.assertFalse(account["missing"])

        checked = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-02T00:10:00Z",
            "--check",
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

        daily = run_cli(
            "daily",
            "--config",
            self.config,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-02T00:10:00Z",
            "--json",
        )
        self.assertEqual(daily.returncode, 0, daily.stderr)
        self.assertFalse(json_output(daily)["accounts"][0]["stale"])


if __name__ == "__main__":
    unittest.main()
