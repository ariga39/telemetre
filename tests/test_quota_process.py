import os
import tempfile
import unittest
from pathlib import Path

try:
    from test_quota_support import (
        json_output,
        pid_is_gone,
        run_cli,
        source,
        write_config,
        write_fake_agy,
        write_fake_codex,
    )
except ImportError:  # running from the repository root
    from tests.test_quota_support import (  # type: ignore
        json_output,
        pid_is_gone,
        run_cli,
        source,
        write_config,
        write_fake_agy,
        write_fake_codex,
    )


class ProcessLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"

    def test_timeout_kills_the_provider_and_its_children(self):
        child_pid_file = self.tmp / "agy-child.pid"
        agy = write_fake_agy(
            self.tmp,
            payload={},
            sleep_seconds=60,
            spawn_child_pidfile=child_pid_file,
        )
        config = write_config(
            self.tmp,
            [source("agy", "personal", agy, timeout_seconds=0.5)],
            database=self.db,
        )
        process = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:00:00Z", "--json"
        )
        self.assertEqual(process.returncode, 1)
        observation = json_output(process)["sampled"][0]
        self.assertEqual(observation["status"], "failure")
        self.assertEqual(observation["error"]["kind"], "timeout")

        child_pid = int(child_pid_file.read_text().strip())
        self.assertTrue(pid_is_gone(child_pid), f"child {child_pid} survived timeout")

    def test_successful_codex_sampling_cleans_up_the_app_server(self):
        child_pid_file = self.tmp / "codex-child.pid"
        codex = write_fake_codex(
            self.tmp,
            result={
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 10,
                            "windowDurationMins": 10080,
                        }
                    }
                }
            },
            hang_after_response=True,
            spawn_child_pidfile=child_pid_file,
        )
        config = write_config(
            self.tmp,
            [source("codex", "work", codex, timeout_seconds=2)],
            database=self.db,
        )
        process = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:00:00Z", "--json"
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json_output(process)["sampled"][0]["status"], "success")

        child_pid = int(child_pid_file.read_text().strip())
        self.assertTrue(pid_is_gone(child_pid), f"child {child_pid} survived success")

    def test_codex_protocol_timeout_is_a_failure_and_kills_the_child(self):
        child_pid_file = self.tmp / "codex-slow.pid"
        codex = write_fake_codex(
            self.tmp,
            result={},
            init_delay_seconds=60,
            spawn_child_pidfile=child_pid_file,
        )
        config = write_config(
            self.tmp,
            [source("codex", "work", codex, timeout_seconds=0.5)],
            database=self.db,
        )
        process = run_cli(
            "sample", "--config", config, "--now", "2026-01-01T00:00:00Z", "--json"
        )
        self.assertEqual(process.returncode, 1)
        observation = json_output(process)["sampled"][0]
        self.assertEqual(observation["error"]["kind"], "timeout")
        child_pid = int(child_pid_file.read_text().strip())
        self.assertTrue(pid_is_gone(child_pid), f"child {child_pid} survived protocol timeout")


if __name__ == "__main__":
    unittest.main()
