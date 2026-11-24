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
        write_fake_codex,
    )
except ImportError:  # running from the repository root
    from tests.test_quota_support import (  # type: ignore
        json_output,
        run_cli,
        source,
        write_config,
        write_fake_agy,
        write_fake_codex,
    )


class AgySamplingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"

    def test_samples_agy_groups_and_reports_their_windows(self):
        payload = {
            "status": "success",
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini",
                            "buckets": [
                                {
                                    "id": "five-hour",
                                    "window": "5h",
                                    "remaining_fraction": 0.42,
                                    "reset_time": "2026-01-01T05:00:00Z",
                                },
                                {
                                    "id": "weekly",
                                    "window": "7d",
                                    "remaining_fraction": 0.9,
                                    "reset_time": "2026-01-07T00:00:00Z",
                                },
                            ],
                        }
                    ]
                }
            },
        }
        agy = write_fake_agy(self.tmp, payload=payload)
        config = write_config(
            self.tmp,
            [source("agy", "personal", agy)],
            database=self.db,
        )

        sampled = run_cli(
            "sample",
            "--config",
            config,
            "--now",
            "2026-01-01T00:00:00Z",
            "--json",
        )
        self.assertEqual(sampled.returncode, 0, sampled.stderr)
        self.assertEqual(json_output(sampled)["sampled"][0]["status"], "success")

        reported = run_cli(
            "report",
            "--config",
            config,
            "--now",
            "2026-01-01T00:01:00Z",
            "--json",
        )
        self.assertEqual(reported.returncode, 0, reported.stderr)
        report = json_output(reported)
        self.assertEqual(report["mode"], "current")
        self.assertEqual(len(report["accounts"]), 1)
        account = report["accounts"][0]
        self.assertEqual(account["provider"], "agy")
        self.assertEqual(account["account"], "personal")
        self.assertEqual(account["last_attempt"]["status"], "success")
        self.assertFalse(account["stale"])
        self.assertEqual(len(account["windows"]), 2)
        five_hour = account["windows"][0]
        self.assertEqual(five_hour["group"], "Gemini")
        self.assertEqual(five_hour["bucket_id"], "five-hour")
        self.assertEqual(five_hour["window"], "5h")
        self.assertEqual(five_hour["remaining"]["source_value"], 0.42)
        self.assertEqual(five_hour["remaining"]["source_unit"], "fraction")
        self.assertEqual(five_hour["remaining"]["display_percent"], 42.0)
        self.assertEqual(five_hour["reset"]["at"], "2026-01-01T05:00:00Z")
        self.assertEqual(five_hour["sampled_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(five_hour["machine"], "test-machine")

    def test_agy_keeps_multiple_model_groups_separate(self):
        payload = {
            "status": "success",
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini",
                            "buckets": [
                                {
                                    "id": "five-hour",
                                    "window": "5h",
                                    "remaining_fraction": 0.7,
                                    "reset_time": "2026-01-01T05:00:00Z",
                                }
                            ],
                        },
                        {
                            "name": "Claude",
                            "buckets": [
                                {
                                    "id": "weekly",
                                    "window": "7d",
                                    "remaining_fraction": 0.3,
                                    "reset_time": "2026-01-07T00:00:00Z",
                                },
                                {
                                    "id": "monthly",
                                    "window": "30d",
                                    "remaining_fraction": 0.8,
                                    "reset_time": "2026-01-31T00:00:00Z",
                                },
                            ],
                        },
                    ]
                }
            },
        }
        agy = write_fake_agy(self.tmp, payload=payload)
        config = write_config(
            self.tmp, [source("agy", "personal", agy)], database=self.db
        )
        sampled = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:00:00Z", "--json"
        )
        self.assertEqual(sampled.returncode, 0, sampled.stderr)
        report = json_output(
            run_cli(
                "report",
                "--config",
                config,
                "--now",
                "2026-01-01T00:01:00Z",
                "--json",
            )
        )
        windows = report["accounts"][0]["windows"]
        self.assertEqual(
            [(window["group"], window["bucket_id"]) for window in windows],
            [("Gemini", "five-hour"), ("Claude", "weekly"), ("Claude", "monthly")],
        )

class CodexInvalidDurationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"

    def test_negative_window_duration_is_a_persisted_source_failure(self):
        agy_payload = {
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
                                    "remaining_fraction": 0.8,
                                    "reset_time": "2026-01-08T00:00:00Z",
                                }
                            ],
                        }
                    ]
                }
            },
        }
        agy = write_fake_agy(self.tmp, payload=agy_payload)
        good_codex = write_fake_codex(
            self.tmp,
            result={
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 10,
                            "windowDurationMins": 10080,
                            "resetsAt": 1767225600,
                        }
                    }
                }
            },
        )
        config = write_config(
            self.tmp,
            [
                source("codex", "work", good_codex),
                source("agy", "personal", agy),
            ],
            database=self.db,
        )
        first = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:00:00Z", "--json"
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        bad_codex = write_fake_codex(
            self.tmp,
            result={
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 20,
                            "windowDurationMins": -60,
                            "resetsAt": 1767225600,
                        }
                    }
                }
            },
        )
        # The configured command path is the same; replace the executable content.
        self.assertEqual(bad_codex, good_codex)
        second = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:15:00Z", "--json"
        )
        self.assertEqual(second.returncode, 1, second.stderr)
        outcomes = {item["provider"]: item for item in json_output(second)["sampled"]}
        self.assertEqual(outcomes["codex"]["status"], "failure")
        self.assertEqual(outcomes["codex"]["error"]["kind"], "invalid_window")
        self.assertEqual(outcomes["agy"]["status"], "success")

        report = json_output(
            run_cli(
                "report",
                "--config",
                config,
                "--now",
                "2026-01-01T00:16:00Z",
                "--json",
            )
        )
        accounts = {item["provider"]: item for item in report["accounts"]}
        self.assertEqual(accounts["codex"]["last_attempt"]["status"], "failure")
        self.assertEqual(
            accounts["codex"]["last_success"]["sampled_at"], "2026-01-01T00:00:00Z"
        )
        self.assertEqual(len(accounts["codex"]["windows"]), 1)
        self.assertEqual(
            accounts["codex"]["windows"][0]["remaining"]["display_percent"], 90.0
        )
        self.assertEqual(
            accounts["agy"]["windows"][0]["remaining"]["display_percent"], 80.0
        )


if __name__ == "__main__":
    unittest.main()


class CodexSamplingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"

    def test_codex_handshake_prefers_limit_map_and_keeps_weekly_window(self):
        result = {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 25,
                        "windowDurationMins": 10080,
                        "resetsAt": 1767225600,
                    },
                }
            },
            "rateLimits": {
                "limitId": "must-not-be-used",
                "primary": {
                    "usedPercent": 99,
                    "windowDurationMins": 300,
                    "resetsAt": 1767225600,
                },
            },
        }
        codex = write_fake_codex(self.tmp, result=result)
        config = write_config(
            self.tmp,
            [source("codex", "work", codex)],
            database=self.db,
        )
        sampled = run_cli(
            "sample", "--config", config, "--now", "2025-12-31T23:00:00Z", "--json"
        )
        self.assertEqual(sampled.returncode, 0, sampled.stderr)

        report = json_output(
            run_cli(
                "report",
                "--config",
                config,
                "--now",
                "2025-12-31T23:01:00Z",
                "--json",
            )
        )
        account = report["accounts"][0]
        self.assertEqual(account["provider"], "codex")
        self.assertEqual(len(account["windows"]), 1)
        window = account["windows"][0]
        self.assertEqual(window["group"], "codex")
        self.assertEqual(window["limit_id"], "codex")
        self.assertEqual(window["bucket_id"], "primary")
        self.assertEqual(window["slot"], "primary")
        self.assertEqual(window["window_minutes"], 10080)
        self.assertEqual(window["window_display"], "7d (10080m)")
        self.assertEqual(window["remaining"]["source_value"], 25.0)
        self.assertEqual(window["remaining"]["source_unit"], "percent_used")
        self.assertEqual(window["remaining"]["display_percent"], 75.0)
        self.assertEqual(window["reset"]["at"], "2026-01-01T00:00:00Z")

    def test_codex_falls_back_to_single_view_and_marks_missing_fields_unknown(self):
        result = {
            "rateLimitsByLimitId": {},
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 0,
                    "windowDurationMins": 300,
                    "resetsAt": 0,
                },
                "secondary": {
                    "windowDurationMins": 10080,
                },
            },
        }
        codex = write_fake_codex(self.tmp, result=result)
        config = write_config(
            self.tmp, [source("codex", "work", codex)], database=self.db
        )
        sampled = run_cli(
            "sample", "--config", config, "--now", "2025-12-31T23:00:00Z", "--json"
        )
        self.assertEqual(sampled.returncode, 0, sampled.stderr)
        report = json_output(
            run_cli(
                "report", "--config", config, "--now", "2025-12-31T23:01:00Z", "--json"
            )
        )
        windows = report["accounts"][0]["windows"]
        self.assertEqual([window["slot"] for window in windows], ["primary", "secondary"])
        primary, secondary = windows
        self.assertEqual(primary["remaining"]["display_percent"], 100.0)
        self.assertEqual(primary["reset"]["at"], None)
        self.assertTrue(primary["reset"]["unknown"])
        self.assertEqual(secondary["remaining"]["display_percent"], None)
        self.assertTrue(secondary["remaining"]["unknown"])
        self.assertIn("missing_remaining", secondary["anomalies"])
        self.assertEqual(secondary["window_minutes"], 10080)
        self.assertEqual(secondary["reset"]["at"], None)


class FailureDoesNotReplaceSnapshotTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"
        self.good_payload = {
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
                                    "remaining_fraction": 0.5,
                                    "reset_time": "2026-01-08T00:00:00Z",
                                }
                            ],
                        }
                    ]
                }
            },
        }

    def config_for(self, command, provider="agy"):
        return write_config(
            self.tmp, [source(provider, "personal", command)], database=self.db
        )

    def test_agy_requires_an_explicit_success_status(self):
        no_status = {
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini",
                            "buckets": [
                                {
                                    "id": "weekly",
                                    "remaining_fraction": 0.5,
                                }
                            ],
                        }
                    ]
                }
            }
        }
        agy = write_fake_agy(self.tmp, payload=no_status)
        config = self.config_for(agy)
        process = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:00:00Z", "--json"
        )
        self.assertEqual(process.returncode, 1)
        observation = json_output(process)["sampled"][0]
        self.assertEqual(observation["status"], "failure")
        self.assertIn(
            observation["error"]["kind"], {"unrecognized_structure", "provider_error"}
        )

    def test_nonfinite_agy_bucket_is_rejected_and_keeps_last_good_snapshot(self):
        agy = write_fake_agy(self.tmp, payload=self.good_payload)
        config = self.config_for(agy)
        first = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:00:00Z", "--json"
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        raw = (
            '{"status":"success","command":{"data":{"groups":['
            '{"name":"Gemini","buckets":['
            '{"id":"valid","remaining_fraction":0.2},'
            '{"id":"broken","remaining_fraction":NaN}'
            ']}]}}}'
        )
        write_fake_agy(self.tmp, raw_stdout=raw)
        second = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:15:00Z", "--json"
        )
        self.assertEqual(second.returncode, 1)
        self.assertEqual(json_output(second)["sampled"][0]["status"], "failure")

        report = json_output(
            run_cli(
                "report", "--config", config, "--now", "2026-01-01T00:16:00Z", "--json"
            )
        )
        account = report["accounts"][0]
        self.assertEqual(account["last_attempt"]["status"], "failure")
        self.assertEqual(account["last_success"]["sampled_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(len(account["windows"]), 1)
        self.assertEqual(account["windows"][0]["remaining"]["display_percent"], 50.0)

    def test_partial_codex_slots_are_rejected_and_keep_last_good_snapshot(self):
        good_result = {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 20,
                        "windowDurationMins": 10080,
                        "resetsAt": 1767225600,
                    }
                }
            }
        }
        codex = write_fake_codex(self.tmp, result=good_result)
        config = self.config_for(codex, provider="codex")
        first = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:00:00Z", "--json"
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        broken_result = {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 30,
                        "windowDurationMins": 10080,
                    },
                    "secondary": "not-an-object",
                }
            }
        }
        write_fake_codex(self.tmp, result=broken_result)
        second = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:15:00Z", "--json"
        )
        self.assertEqual(second.returncode, 1)
        self.assertEqual(json_output(second)["sampled"][0]["status"], "failure")

        report = json_output(
            run_cli(
                "report", "--config", config, "--now", "2026-01-01T00:16:00Z", "--json"
            )
        )
        account = report["accounts"][0]
        self.assertEqual(account["last_attempt"]["status"], "failure")
        self.assertEqual(account["last_success"]["sampled_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(len(account["windows"]), 1)
        self.assertEqual(account["windows"][0]["remaining"]["display_percent"], 80.0)


class WindowAndFailureSemanticsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"

    def test_zero_and_overage_keep_source_values_and_floor_display(self):
        agy_payload = {
            "status": "success",
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini",
                            "buckets": [
                                {
                                    "id": "zero",
                                    "window": "5h",
                                    "remaining_fraction": 0,
                                    "reset_time": "2026-01-01T05:00:00Z",
                                },
                                {
                                    "id": "over",
                                    "window": "7d",
                                    "remaining_fraction": -0.2,
                                    "reset_time": "2026-01-07T00:00:00Z",
                                },
                            ],
                        }
                    ]
                }
            },
        }
        agy = write_fake_agy(self.tmp, payload=agy_payload)
        config = write_config(
            self.tmp, [source("agy", "personal", agy)], database=self.db
        )
        self.assertEqual(
            run_cli(
                "sample",
                "--config",
                config,
                "--now",
                "2026-01-01T00:00:00Z",
                "--json",
            ).returncode,
            0,
        )
        report = json_output(
            run_cli(
                "report",
                "--config",
                config,
                "--now",
                "2026-01-01T00:01:00Z",
                "--json",
            )
        )
        windows = {window["bucket_id"]: window for window in report["accounts"][0]["windows"]}
        self.assertEqual(windows["zero"]["remaining"]["source_value"], 0.0)
        self.assertEqual(windows["zero"]["remaining"]["display_percent"], 0.0)
        self.assertEqual(windows["over"]["remaining"]["source_value"], -0.2)
        self.assertEqual(windows["over"]["remaining"]["display_percent"], 0.0)
        self.assertIn("remaining_out_of_range", windows["over"]["anomalies"])

    def test_codex_overage_floors_display_and_flags_the_source_value(self):
        codex = write_fake_codex(
            self.tmp,
            result={
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 120,
                            "windowDurationMins": 10080,
                            "resetsAt": 1767225600,
                        }
                    }
                }
            },
        )
        config = write_config(
            self.tmp, [source("codex", "work", codex)], database=self.db
        )
        self.assertEqual(
            run_cli(
                "sample",
                "--config",
                config,
                "--now",
                "2026-01-01T00:00:00Z",
                "--json",
            ).returncode,
            0,
        )
        report = json_output(
            run_cli(
                "report",
                "--config",
                config,
                "--now",
                "2026-01-01T00:01:00Z",
                "--json",
            )
        )
        window = report["accounts"][0]["windows"][0]
        self.assertEqual(window["remaining"]["source_value"], 120.0)
        self.assertEqual(window["remaining"]["display_percent"], 0.0)
        self.assertIn("remaining_out_of_range", window["anomalies"])

    def test_empty_window_set_is_success_and_reported_as_no_windows(self):
        agy = write_fake_agy(
            self.tmp,
            payload={
                "status": "success",
                "command": {"data": {"groups": []}},
            },
        )
        config = write_config(
            self.tmp, [source("agy", "personal", agy)], database=self.db
        )
        sampled = run_cli(
            "sample",
            "--config",
            config,
            "--now",
            "2026-01-01T00:00:00Z",
            "--json",
        )
        self.assertEqual(sampled.returncode, 0, sampled.stderr)
        self.assertTrue(json_output(sampled)["sampled"][0]["empty_windows"])
        report = json_output(
            run_cli(
                "report",
                "--config",
                config,
                "--now",
                "2026-01-01T00:01:00Z",
                "--json",
            )
        )
        self.assertTrue(report["accounts"][0]["empty_windows"])
        self.assertEqual(report["accounts"][0]["windows"], [])
        text = run_cli(
            "report", "--config", config, "--now", "2026-01-01T00:01:00Z"
        )
        self.assertIn("no windows returned", text.stdout)

    def test_one_provider_failure_does_not_block_the_other_provider(self):
        agy = write_fake_agy(self.tmp, payload={}, exit_code=1, stderr="login required")
        codex = write_fake_codex(
            self.tmp,
            result={
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 10,
                            "windowDurationMins": 10080,
                            "resetsAt": 1767225600,
                        }
                    }
                }
            },
        )
        config = write_config(
            self.tmp,
            [source("agy", "personal", agy), source("codex", "work", codex)],
            database=self.db,
        )
        sampled = run_cli(
            "sample",
            "--config",
            config,
            "--now",
            "2026-01-01T00:00:00Z",
            "--json",
        )
        self.assertEqual(sampled.returncode, 1)
        outcomes = {item["provider"]: item for item in json_output(sampled)["sampled"]}
        self.assertEqual(outcomes["agy"]["status"], "failure")
        self.assertEqual(outcomes["codex"]["status"], "success")

        report = json_output(
            run_cli(
                "report",
                "--config",
                config,
                "--now",
                "2026-01-01T00:01:00Z",
                "--json",
            )
        )
        accounts = {item["provider"]: item for item in report["accounts"]}
        self.assertEqual(accounts["agy"]["last_attempt"]["status"], "failure")
        self.assertEqual(len(accounts["agy"]["windows"]), 0)
        self.assertEqual(len(accounts["codex"]["windows"]), 1)
        self.assertEqual(
            accounts["codex"]["windows"][0]["remaining"]["display_percent"], 90.0
        )
