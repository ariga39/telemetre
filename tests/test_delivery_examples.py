"""Regression checks for the scheduling examples in the operations guide."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def _timeout_seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)(min|s)", value.strip())
    if not match:
        raise AssertionError(f"unsupported timeout value: {value!r}")
    amount = int(match.group(1))
    return amount * 60 if match.group(2) == "min" else amount


def _outer_timeout(text: str) -> int:
    match = re.search(r"^TimeoutStartSec=(\S+)$", text, re.MULTILINE)
    if not match:
        raise AssertionError("missing TimeoutStartSec")
    return _timeout_seconds(match.group(1))


def _inner_timeouts(text: str) -> list[int]:
    return [int(value) for value in re.findall(r"--timeout\s+(\d+)", text)]


class SchedulingExampleTest(unittest.TestCase):
    def test_cron_locks_live_under_home_and_quota_sampling_is_bounded(self) -> None:
        text = (EXAMPLES / "cron" / "telemetre.crontab").read_text(encoding="utf-8")
        self.assertNotIn("$XDG_RUNTIME_DIR", text)
        locks = re.findall(r'--lock\s+"([^"]+)"', text)
        self.assertTrue(locks)
        for lock in locks:
            self.assertTrue(lock.startswith("$HOME/"), lock)

        sample_lines = [
            line for line in text.splitlines() if "-m telemetre.quota sample" in line
        ]
        self.assertEqual(len(sample_lines), 1)
        sample_line = sample_lines[0]
        self.assertIn("-m telemetre.delivery run", sample_line)
        self.assertIn("--lock", sample_line)
        self.assertRegex(sample_line, r"--timeout\s+300")

    def test_source_systemd_outer_timeout_exceeds_summed_inner_budgets(self) -> None:
        for name, expected_inner_total in (
            ("telemetre-collect.service", 1200),
            ("telemetre-quota.service", 900),
        ):
            text = (EXAMPLES / "systemd" / name).read_text(encoding="utf-8")
            inner = _inner_timeouts(text)
            self.assertEqual(sum(inner), expected_inner_total, name)
            self.assertGreater(_outer_timeout(text), sum(inner), name)

        quota_service = (EXAMPLES / "systemd" / "telemetre-quota.service").read_text(
            encoding="utf-8"
        )
        sample_line = [
            line
            for line in quota_service.splitlines()
            if "-m telemetre.quota sample" in line
        ][0]
        self.assertIn("-m telemetre.delivery run", sample_line)
        self.assertIn("--lock", sample_line)
        self.assertIn("--timeout 300", sample_line)


if __name__ == "__main__":
    unittest.main()
