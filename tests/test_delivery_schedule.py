"""Scheduling checks: one instance per job and a hard timeout."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from telemetre.delivery_schedule import run_locked_job
from tests.test_delivery_support import pid_is_gone


class LockedJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.lock = self.tmp / "job.lock"

    def test_second_instance_is_skipped_while_the_first_holds_the_lock(self) -> None:
        holder = self.lock.open("a+")
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = self.tmp / "must-not-run"
        result = run_locked_job(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
            lock_path=self.lock,
            timeout=5,
        )
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "already running")
        self.assertFalse(marker.exists())
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

    def test_lock_is_released_after_a_normal_or_failing_run(self) -> None:
        first = run_locked_job(
            [sys.executable, "-c", "raise SystemExit(7)"],
            lock_path=self.lock,
            timeout=5,
        )
        self.assertTrue(first["ran"])
        self.assertEqual(first["exit_code"], 7)
        second = run_locked_job(
            [sys.executable, "-c", "raise SystemExit(0)"],
            lock_path=self.lock,
            timeout=5,
        )
        self.assertTrue(second["ran"])
        self.assertEqual(second["exit_code"], 0)

    def test_hard_timeout_kills_the_job_and_its_children(self) -> None:
        pid_file = self.tmp / "child.pid"
        source = textwrap.dedent(
            f"""
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(pid_file)!r}, "w").write(str(child.pid))
            time.sleep(60)
            """
        )
        started = time.monotonic()
        result = run_locked_job(
            [sys.executable, "-c", source],
            lock_path=self.lock,
            timeout=0.5,
        )
        elapsed = time.monotonic() - started
        self.assertTrue(result["ran"])
        self.assertTrue(result["timed_out"])
        self.assertLess(elapsed, 8)
        pid = int(pid_file.read_text())
        self.assertTrue(pid_is_gone(pid))

    def test_missing_command_is_reported_without_raising(self) -> None:
        result = run_locked_job(
            ["/nonexistent/job-command"],
            lock_path=self.lock,
            timeout=5,
        )
        self.assertTrue(result["ran"])
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("cannot start", result["error"])


if __name__ == "__main__":
    unittest.main()
