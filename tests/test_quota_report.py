import json
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


def agy_payload(remaining: float, reset: str = "2026-01-08T00:00:00Z"):
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


class CurrentReportSemanticsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"
        self.agy = write_fake_agy(self.tmp, payload=agy_payload(0.6))
        self.config = write_config(
            self.tmp,
            [source("agy", "personal", self.agy, interval_seconds=900)],
            database=self.db,
        )

    def sample_at(self, when: str, payload):
        write_fake_agy(self.tmp, payload=payload)
        process = run_cli(
            "sample", "--config", self.config, "--now", when, "--json"
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return process

    def report_at(self, when: str):
        process = run_cli(
            "report", "--config", self.config, "--now", when, "--json"
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return json_output(process)

    def test_reports_net_change_only_within_same_window_and_refresh(self):
        self.sample_at("2026-01-01T00:00:00Z", agy_payload(0.6))
        self.sample_at("2026-01-01T00:15:00Z", agy_payload(0.4))
        report = self.report_at("2026-01-01T00:16:00Z")
        window = report["accounts"][0]["windows"][0]
        self.assertTrue(window["change"]["comparable"])
        self.assertEqual(window["change"]["previous_percent"], 60.0)
        self.assertAlmostEqual(window["change"]["delta_percent"], -20.0)
        self.assertEqual(
            window["change"]["previous_sampled_at"], "2026-01-01T00:00:00Z"
        )

        self.sample_at(
            "2026-01-01T00:30:00Z",
            agy_payload(0.8, reset="2026-01-15T00:00:00Z"),
        )
        report = self.report_at("2026-01-01T00:31:00Z")
        window = report["accounts"][0]["windows"][0]
        self.assertEqual(window["change"]["comparable"], False)
        self.assertEqual(window["change"]["reason"], "window_refreshed")

    def test_keeps_last_success_windows_when_the_latest_attempt_fails(self):
        self.sample_at("2026-01-01T00:00:00Z", agy_payload(0.4))
        write_fake_agy(self.tmp, payload={}, exit_code=1, stderr="Please login")
        process = run_cli(
            "sample", "--config", self.config, "--now", "2026-01-01T00:15:00Z", "--json"
        )
        self.assertEqual(process.returncode, 1)
        observation = json_output(process)["sampled"][0]
        self.assertEqual(observation["error"]["kind"], "not_authenticated")

        report = self.report_at("2026-01-01T00:16:00Z")
        account = report["accounts"][0]
        self.assertEqual(account["last_attempt"]["status"], "failure")
        self.assertEqual(account["last_attempt"]["error_kind"], "not_authenticated")
        self.assertEqual(account["last_success"]["sampled_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(len(account["windows"]), 1)
        self.assertEqual(account["windows"][0]["remaining"]["display_percent"], 40.0)

    def test_marks_stale_readings_and_pending_resets_without_inventing_values(self):
        self.sample_at(
            "2026-01-01T00:00:00Z",
            agy_payload(0.5, reset="2026-01-01T01:00:00Z"),
        )
        report = self.report_at("2026-01-01T02:00:00Z")
        account = report["accounts"][0]
        self.assertFalse(account["missing"])
        self.assertTrue(account["stale"])
        self.assertEqual(account["last_success"]["sampled_at"], "2026-01-01T00:00:00Z")
        window = account["windows"][0]
        self.assertTrue(window["freshness"]["stale"])
        self.assertTrue(window["freshness"]["pending_refresh"])
        self.assertEqual(window["remaining"]["display_percent"], 50.0)

    def test_breaks_net_change_comparison_across_a_sampling_gap(self):
        self.sample_at("2026-01-01T00:00:00Z", agy_payload(0.6))
        self.sample_at("2026-01-01T00:15:00Z", agy_payload(0.5))
        self.sample_at("2026-01-01T01:00:00Z", agy_payload(0.4))
        report = self.report_at("2026-01-01T01:01:00Z")
        window = report["accounts"][0]["windows"][0]
        self.assertEqual(window["change"]["comparable"], False)
        self.assertEqual(window["change"]["reason"], "sampling_gap")

    def test_check_exits_nonzero_for_missing_stale_or_failed_sources(self):
        missing = run_cli(
            "report", "--config", self.config, "--now", "2026-01-01T00:00:00Z", "--check"
        )
        self.assertEqual(missing.returncode, 3)

        self.sample_at("2026-01-01T00:00:00Z", agy_payload(0.5))
        healthy = run_cli(
            "report", "--config", self.config, "--now", "2026-01-01T00:01:00Z", "--check"
        )
        self.assertEqual(healthy.returncode, 0, healthy.stderr)

        stale = run_cli(
            "report", "--config", self.config, "--now", "2026-01-01T02:00:00Z", "--check"
        )
        self.assertEqual(stale.returncode, 3)

    def test_future_date_check_does_not_pass_an_expected_account_without_coverage(self):
        self.sample_at("2026-01-01T23:45:00Z", agy_payload(0.5))
        report_process = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-02",
            "--now",
            "2026-01-01T23:50:00Z",
            "--json",
        )
        self.assertEqual(report_process.returncode, 0, report_process.stderr)
        account = json_output(report_process)["accounts"][0]
        self.assertTrue(account["missing"])

        checked = run_cli(
            "report",
            "--config",
            self.config,
            "--date",
            "2026-01-02",
            "--now",
            "2026-01-01T23:50:00Z",
            "--check",
        )
        self.assertEqual(checked.returncode, 3, checked.stderr)

    def test_check_ignores_db_only_history_after_a_source_is_removed_from_config(self):
        write_fake_agy(self.tmp, payload=agy_payload(0.6))
        both = write_config(
            self.tmp,
            [
                source("agy", "personal", self.agy, interval_seconds=900),
                source("agy", "retired", self.agy, interval_seconds=900),
            ],
            database=self.db,
        )
        first = run_cli(
            "sample", "--config", both, "--now", "2026-01-01T00:00:00Z", "--json"
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        current = write_config(
            self.tmp,
            [source("agy", "personal", self.agy, interval_seconds=900)],
            database=self.db,
        )
        write_fake_agy(self.tmp, payload=agy_payload(0.4))
        second = run_cli(
            "sample", "--config", current, "--now", "2026-01-01T01:00:00Z", "--json"
        )
        self.assertEqual(second.returncode, 0, second.stderr)

        report_process = run_cli(
            "report",
            "--config",
            current,
            "--now",
            "2026-01-01T01:01:00Z",
            "--json",
        )
        self.assertEqual(report_process.returncode, 0, report_process.stderr)
        accounts = {
            account["account"]: account
            for account in json_output(report_process)["accounts"]
        }
        self.assertTrue(accounts["personal"]["expected"])
        self.assertFalse(accounts["retired"]["expected"])
        self.assertEqual(len(accounts["retired"]["windows"]), 1)
        self.assertEqual(
            accounts["retired"]["windows"][0]["remaining"]["display_percent"], 60.0
        )

        checked = run_cli(
            "report",
            "--config",
            current,
            "--now",
            "2026-01-01T01:01:00Z",
            "--check",
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

        empty = write_config(self.tmp, [], database=self.db)
        removed = run_cli(
            "report",
            "--config",
            empty,
            "--now",
            "2026-01-01T01:01:00Z",
            "--json",
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        removed_accounts = {
            account["account"]: account
            for account in json_output(removed)["accounts"]
        }
        self.assertEqual(set(removed_accounts), {"personal", "retired"})
        self.assertTrue(all(not account["expected"] for account in removed_accounts.values()))
        removed_check = run_cli(
            "report",
            "--config",
            empty,
            "--now",
            "2026-01-01T01:01:00Z",
            "--check",
        )
        self.assertEqual(removed_check.returncode, 0, removed_check.stderr)


if __name__ == "__main__":
    unittest.main()
