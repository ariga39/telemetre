"""Health reporting for expected-but-missing usage and quota sources."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telemetre import db, quota
from telemetre.delivery_health import check_health
from telemetre.timeutil import utc_text


def _usage_status(center: Path, *, machine: str = "alpha", account: str = "acct", source_missing: bool = False) -> None:
    conn = db.connect(center)
    try:
        db.init_center_schema(conn)
        now = "2026-11-02T00:00:00Z"
        conn.execute(
            "INSERT INTO machine_status (machine, account, kind, last_run_id, last_success_at, "
            "last_scan_json, collector_version, updated_at) VALUES (?, ?, 'agy_usage', 'run', ?, ?, 'test', ?)",
            (
                machine,
                account,
                now,
                json.dumps({"source_missing": int(source_missing)}),
                now,
            ),
        )
    finally:
        conn.close()


def _quota_sample(
    center: Path,
    *,
    sample_id: str,
    machine: str = "alpha",
    account: str = "acct",
    status: str = "success",
    sampled_at: str = "2026-11-02T00:00:00Z",
) -> None:
    observation = quota.Observation(
        provider="agy",
        account=account,
        machine=machine,
        sampled_at=sampled_at,
        status=status,
        windows=(quota.WindowObservation(group="Gemini", bucket_id="five-hour"),)
        if status == "success"
        else (),
        error_kind=None if status == "success" else "timeout",
        error_detail=None if status == "success" else "provider timed out",
        sample_id=sample_id,
    )
    quota.ingest_observation(center, observation, received_at="2026-11-02T00:00:01Z")


class HealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.center = Path(self._tmp.name) / "center.sqlite3"
        self.now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)

    def check(self, **kwargs):
        return check_health(self.center, now=self.now, **kwargs)

    def test_expected_sources_that_have_never_reported_fail(self) -> None:
        status = self.check(
            expected_usage=[("alpha", "acct")],
            expected_quota=[("alpha", "agy", "acct")],
        )
        self.assertFalse(status["ok"])
        self.assertEqual(len(status["checks"]), 2)
        for check in status["checks"]:
            self.assertFalse(check["ok"])
            self.assertEqual(check["reason"], "no successful scan reported")

    def test_missing_usage_source_and_missing_quota_source_are_reported_separately(self) -> None:
        _usage_status(self.center, source_missing=True)
        status = self.check(
            expected_usage=[("alpha", "acct"), ("beta", "acct")],
            expected_quota=[("alpha", "agy", "acct")],
        )
        self.assertFalse(status["ok"])
        by_key = {(item["kind"], item["machine"]): item for item in status["checks"]}
        self.assertEqual(
            by_key[("agy_usage", "alpha")]["reason"], "expected source missing"
        )
        self.assertEqual(
            by_key[("agy_usage", "beta")]["reason"], "no successful scan reported"
        )
        self.assertEqual(
            by_key[("quota_sample", "alpha")]["reason"], "no successful scan reported"
        )

    def test_recent_usage_and_quota_sources_make_the_check_ok(self) -> None:
        _usage_status(self.center)
        _quota_sample(self.center, sample_id="q1")
        status = self.check(
            expected_usage=[("alpha", "acct")],
            expected_quota=[("alpha", "agy", "acct")],
            max_age_seconds=7200,
        )
        self.assertTrue(status["ok"], status)
        self.assertTrue(all(item["ok"] for item in status["checks"]))

    def test_latest_quota_failure_or_stale_sample_fails_the_check(self) -> None:
        _usage_status(self.center)
        _quota_sample(self.center, sample_id="q1", status="failure")
        failed = self.check(expected_usage=[("alpha", "acct")], expected_quota=[("alpha", "agy", "acct")])
        self.assertFalse(failed["ok"])
        quota_check = [item for item in failed["checks"] if item["kind"] == "quota_sample"][0]
        self.assertIn("failed", quota_check["reason"])

        self.center.unlink()
        _usage_status(self.center)
        _quota_sample(
            self.center,
            sample_id="old",
            sampled_at="2026-11-01T00:00:00Z",
        )
        stale = self.check(
            expected_usage=[("alpha", "acct")],
            expected_quota=[("alpha", "agy", "acct")],
            max_age_seconds=3600,
        )
        self.assertFalse(stale["ok"])
        quota_check = [item for item in stale["checks"] if item["kind"] == "quota_sample"][0]
        self.assertEqual(quota_check["reason"], "stale sample")

    def test_existing_usage_status_is_included_when_no_expectation_is_listed(self) -> None:
        _usage_status(self.center)
        status = self.check()
        self.assertTrue(status["ok"])
        self.assertEqual(status["checks"][0]["kind"], "agy_usage")
        self.assertEqual(status["checked_at"], utc_text(self.now))


if __name__ == "__main__":
    unittest.main()
