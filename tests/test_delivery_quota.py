"""Quota delivery adapter: public read seam, one sample per send, retry ledger."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from telemetre import quota
from telemetre.delivery_bindings import Binding, load_bindings
from telemetre.delivery_quota import (
    DeliveryQuotaError,
    build_next_batch,
    replay_pending,
    sync_outbox,
)
from telemetre.delivery_receive import receive_bytes
from tests.test_delivery_support import (
    delivery_json,
    run_delivery_cli,
    set_fake_mode,
    ssh_cli_args,
    write_bindings,
    write_fake_ssh,
)

BINDING = Binding(
    binding_id="alpha-quota",
    machine="alpha",
    account="acct@example.test",
    kind="quota_sample",
    provider="agy",
)


def _observation(sample_id: str, *, status: str = "success") -> quota.Observation:
    windows = (
        (
            quota.WindowObservation(
                group="Gemini",
                bucket_id="five-hour",
                window="5h",
                window_minutes=300,
                remaining_value=0.5,
                remaining_unit="fraction",
                remaining_display=50.0,
                reset_at="2026-11-02T05:00:00Z",
            ),
        )
        if status == "success"
        else ()
    )
    return quota.Observation(
        provider="agy",
        account="acct@example.test",
        machine="alpha",
        sampled_at="2026-11-02T00:00:00Z",
        status=status,
        windows=windows,
        error_kind=None if status == "success" else "timeout",
        error_detail=None if status == "success" else "provider timed out",
        sample_id=sample_id,
    )


class _DirectTransport:
    def __init__(self, center: Path, bindings, binding_id: str = "alpha-quota"):
        self.center = center
        self.bindings = bindings
        self.binding_id = binding_id
        self.calls = 0

    def send(self, payload: bytes) -> dict:
        self.calls += 1
        return receive_bytes(
            self.center,
            self.bindings,
            self.binding_id,
            payload,
        )


class _LostAckTransport(_DirectTransport):
    def send(self, payload: bytes) -> dict:
        self.calls += 1
        receive_bytes(self.center, self.bindings, "alpha-quota", payload)
        raise RuntimeError("acknowledgement lost after commit")


class _BadAckTransport:
    def __init__(self, ack: dict):
        self.ack = ack

    def send(self, payload: bytes) -> dict:
        return self.ack


class _BadAckAfterCommitTransport(_DirectTransport):
    def __init__(self, center: Path, bindings, ack: dict):
        super().__init__(center, bindings)
        self.ack = ack

    def send(self, payload: bytes) -> dict:
        self.calls += 1
        receive_bytes(self.center, self.bindings, "alpha-quota", payload)
        return self.ack


def _valid_ack() -> dict:
    return {
        "ok": True,
        "kind": "quota_sample",
        "machine": "alpha",
        "account": "acct@example.test",
        "provider": "agy",
        "accepted": 1,
        "stored": 1,
        "duplicates": 0,
        "conflicts": 0,
    }


class QuotaDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.local = self.tmp / "quota.sqlite3"
        self.center = self.tmp / "center.sqlite3"
        self.bindings = load_bindings(
            write_bindings(
                self.tmp / "bindings.json",
                {
                    "alpha-quota": {
                        "machine": "alpha",
                        "account": "acct@example.test",
                        "kind": "quota_sample",
                        "provider": "agy",
                    }
                },
            )
        )
        quota.ingest_observation(self.local, _observation("sample-1"))

    def test_outbox_keeps_only_ids_and_sends_one_original_observation_at_a_time(self) -> None:
        self.assertEqual(sync_outbox(self.local, BINDING), 1)
        self.assertEqual(sync_outbox(self.local, BINDING), 0)
        conn = quota.connect_database(self.local)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(delivery_quota_outbox)")]
            self.assertNotIn("payload", columns)
        finally:
            conn.close()

        batch = build_next_batch(self.local, BINDING)
        payload = json.loads(batch.payload)
        self.assertEqual(batch.sample_id, "sample-1")
        self.assertEqual(len(payload["observations"]), 1)
        self.assertEqual(payload["observations"][0]["sample_id"], "sample-1")
        self.assertEqual(
            payload["observations"][0]["windows"][0]["remaining"]["display_percent"],
            50.0,
        )

    def test_successful_delivery_marks_the_ledger_and_is_idempotent(self) -> None:
        transport = _DirectTransport(self.center, self.bindings)
        first = replay_pending(self.local, transport, binding=BINDING)
        self.assertEqual(first["sent_observations"], 1)
        self.assertEqual(first["delivered"], 1)
        self.assertEqual(first["duplicates"], 0)
        second = replay_pending(self.local, transport, binding=BINDING)
        self.assertEqual(second["sent_observations"], 0)
        self.assertEqual(second["delivered"], 0)

        conn = sqlite3.connect(self.center)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM quota_samples").fetchone()[0], 1
            )
        finally:
            conn.close()

        quota.ingest_observation(self.local, _observation("sample-2", status="failure"))
        third = replay_pending(self.local, transport, binding=BINDING)
        self.assertEqual(third["synced"], 1)
        self.assertEqual(third["sent_observations"], 1)
        conn = sqlite3.connect(self.center)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM quota_samples").fetchone()[0], 2
            )
        finally:
            conn.close()

    def test_ack_loss_after_commit_retries_the_same_sample_without_duplication(self) -> None:
        with self.assertRaises(RuntimeError):
            replay_pending(
                self.local,
                _LostAckTransport(self.center, self.bindings),
                binding=BINDING,
            )
        self.assertIsNotNone(build_next_batch(self.local, BINDING))

        result = replay_pending(
            self.local,
            _DirectTransport(self.center, self.bindings),
            binding=BINDING,
        )
        self.assertEqual(result["sent_observations"], 1)
        self.assertEqual(result["duplicates"], 1)
        conn = sqlite3.connect(self.center)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM quota_samples").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_bad_ack_counters_keep_the_pending_sample(self) -> None:
        bad_acks = []
        for change in (
            {"stored": None},
            {"duplicates": None},
            {"stored": True},
            {"duplicates": "0"},
            {"stored": 0, "duplicates": 0},
            {"accepted": 0, "stored": 0, "duplicates": 0},
            {"accepted": None},
            {"ok": False},
        ):
            ack = _valid_ack()
            ack.update(change)
            bad_acks.append(ack)

        for ack in bad_acks:
            with self.subTest(ack=ack):
                with self.assertRaises(DeliveryQuotaError):
                    replay_pending(
                        self.local,
                        _BadAckTransport(ack),
                        binding=BINDING,
                    )
                self.assertIsNotNone(build_next_batch(self.local, BINDING))
                conn = sqlite3.connect(self.local)
                try:
                    delivered = conn.execute(
                        "SELECT delivered_at FROM delivery_quota_outbox "
                        "WHERE sample_id = 'sample-1'"
                    ).fetchone()[0]
                finally:
                    conn.close()
                self.assertIsNone(delivered)

    def test_bad_ack_after_commit_retries_without_duplication(self) -> None:
        with self.assertRaises(DeliveryQuotaError):
            replay_pending(
                self.local,
                _BadAckAfterCommitTransport(
                    self.center,
                    self.bindings,
                    {"ok": True, "accepted": 1, "stored": 1},
                ),
                binding=BINDING,
            )
        self.assertIsNotNone(build_next_batch(self.local, BINDING))
        conn = sqlite3.connect(self.center)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM quota_samples").fetchone()[0], 1
            )
        finally:
            conn.close()

        result = replay_pending(
            self.local,
            _DirectTransport(self.center, self.bindings),
            binding=BINDING,
        )
        self.assertEqual(result["sent_observations"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertIsNone(build_next_batch(self.local, BINDING))

    def test_invalid_window_failure_is_delivered_with_its_category(self) -> None:
        quota.ingest_observation(
            self.local,
            quota.Observation(
                provider="agy",
                account="acct@example.test",
                machine="alpha",
                sampled_at="2026-11-02T00:01:00Z",
                status="failure",
                error_kind="invalid_window",
                error_detail="window is outside the supported range",
                sample_id="sample-invalid-window",
            ),
        )
        replay_pending(
            self.local,
            _DirectTransport(self.center, self.bindings),
            binding=BINDING,
        )
        stored = {
            observation.sample_id: observation
            for observation in quota.iter_observations(self.center)
        }
        self.assertIn("sample-invalid-window", stored)
        self.assertEqual(stored["sample-invalid-window"].error_kind, "invalid_window")
        self.assertEqual(
            stored["sample-invalid-window"].error_detail,
            "provider window is outside the supported range",
        )

    def test_oversized_observation_is_retained_with_a_clear_error(self) -> None:
        with self.assertRaises(DeliveryQuotaError) as raised:
            replay_pending(
                self.local,
                _DirectTransport(self.center, self.bindings),
                binding=BINDING,
                max_bytes=80,
            )
        self.assertIn("sample retained", str(raised.exception))
        self.assertIsNotNone(build_next_batch(self.local, BINDING))


def _identity_observation(
    machine: str,
    provider: str,
    account: str,
    sample_id: str,
    *,
    sampled_at: str,
) -> quota.Observation:
    if provider == "agy":
        window = quota.WindowObservation(
            group="Gemini",
            bucket_id="five-hour",
            window="5h",
            window_minutes=300,
            remaining_value=0.5,
            remaining_unit="fraction",
            remaining_display=50.0,
            reset_at="2026-11-02T05:00:00Z",
        )
    else:
        window = quota.WindowObservation(
            group="codex",
            bucket_id="primary",
            slot="primary",
            window_minutes=10080,
            remaining_value=25.0,
            remaining_unit="percent_used",
            remaining_display=75.0,
            reset_at="2026-11-08T00:00:00Z",
        )
    return quota.Observation(
        provider=provider,
        account=account,
        machine=machine,
        sampled_at=sampled_at,
        status="success",
        windows=(window,),
        sample_id=sample_id,
    )


class LegacyOutboxTest(unittest.TestCase):
    """Old ID-only outboxes remain usable without a destructive migration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.local = self.tmp / "quota.sqlite3"
        self.center = self.tmp / "center.sqlite3"
        self.bindings = load_bindings(
            write_bindings(
                self.tmp / "bindings.json",
                {
                    "alpha-agy": {
                        "machine": "alpha",
                        "account": "acct@example.test",
                        "kind": "quota_sample",
                        "provider": "agy",
                    },
                    "alpha-codex": {
                        "machine": "alpha",
                        "account": "acct@example.test",
                        "kind": "quota_sample",
                        "provider": "codex",
                    },
                },
            )
        )
        for sample_id, provider in (
            ("legacy-agy", "agy"),
            ("legacy-agy-done", "agy"),
            ("legacy-codex", "codex"),
        ):
            quota.ingest_observation(
                self.local,
                _identity_observation(
                    "alpha",
                    provider,
                    "acct@example.test",
                    sample_id,
                    sampled_at="2026-11-02T00:00:00Z",
                ),
            )
        conn = sqlite3.connect(self.local)
        try:
            conn.execute(
                "CREATE TABLE delivery_quota_outbox ("
                "sample_id TEXT PRIMARY KEY, enqueued_at TEXT NOT NULL, "
                "attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT, "
                "last_error TEXT, delivered_at TEXT)"
            )
            conn.execute(
                "INSERT INTO delivery_quota_outbox (sample_id, enqueued_at) VALUES (?, ?)",
                ("legacy-agy", "2026-11-02T00:01:00Z"),
            )
            conn.execute(
                "INSERT INTO delivery_quota_outbox (sample_id, enqueued_at, delivered_at) "
                "VALUES (?, ?, ?)",
                ("legacy-agy-done", "2026-11-02T00:02:00Z", "2026-11-02T00:03:00Z"),
            )
            conn.execute(
                "INSERT INTO delivery_quota_outbox (sample_id, enqueued_at) VALUES (?, ?)",
                ("legacy-codex", "2026-11-02T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    def _send(self, binding_id: str) -> dict:
        return replay_pending(
            self.local,
            _DirectTransport(self.center, self.bindings, binding_id),
            binding=self.bindings[binding_id],
        )

    def test_old_pending_and_delivered_rows_remain_usable(self) -> None:
        first = self._send("alpha-agy")
        self.assertEqual(first["sent_observations"], 1)
        self.assertEqual(
            [item.sample_id for item in quota.iter_observations(self.center)],
            ["legacy-agy"],
        )
        conn = sqlite3.connect(self.local)
        try:
            delivered = dict(
                conn.execute(
                    "SELECT sample_id, delivered_at FROM delivery_quota_outbox"
                ).fetchall()
            )
        finally:
            conn.close()
        self.assertIsNotNone(delivered["legacy-agy"])
        self.assertIsNotNone(delivered["legacy-agy-done"])
        self.assertIsNone(delivered["legacy-codex"])

        second = self._send("alpha-codex")
        self.assertEqual(second["sent_observations"], 1)
        self.assertEqual(
            sorted(item.sample_id for item in quota.iter_observations(self.center)),
            ["legacy-agy", "legacy-codex"],
        )


class MixedIdentityBacklogTest(unittest.TestCase):
    """One local quota DB can hold several machines/providers/accounts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.local = self.tmp / "quota.sqlite3"
        self.center = self.tmp / "center.sqlite3"
        self.bindings_path = write_bindings(
            self.tmp / "bindings.json",
            {
                "alpha-agy": {
                    "machine": "alpha",
                    "account": "account-a@example.test",
                    "kind": "quota_sample",
                    "provider": "agy",
                },
                "alpha-codex": {
                    "machine": "alpha",
                    "account": "account-a@example.test",
                    "kind": "quota_sample",
                    "provider": "codex",
                },
                "beta-agy": {
                    "machine": "beta",
                    "account": "account-b@example.test",
                    "kind": "quota_sample",
                    "provider": "agy",
                },
                "beta-codex": {
                    "machine": "beta",
                    "account": "account-b@example.test",
                    "kind": "quota_sample",
                    "provider": "codex",
                },
            },
        )
        # The globally oldest sample is beta/codex, not alpha/agy.
        samples = [
            ("beta", "codex", "account-b@example.test", "beta-codex-1", "2026-11-02T00:00:00Z"),
            ("alpha", "agy", "account-a@example.test", "alpha-agy-1", "2026-11-02T00:01:00Z"),
            ("beta", "agy", "account-b@example.test", "beta-agy-1", "2026-11-02T00:02:00Z"),
            ("alpha", "codex", "account-a@example.test", "alpha-codex-1", "2026-11-02T00:03:00Z"),
        ]
        for machine, provider, account, sample_id, sampled_at in samples:
            quota.ingest_observation(
                self.local,
                _identity_observation(machine, provider, account, sample_id, sampled_at=sampled_at),
            )

    def _send(self, binding_id: str, *, mode: str = "ok"):
        script = write_fake_ssh(
            self.tmp,
            name=f"mixed-{binding_id}",
            binding_id=binding_id,
            bindings_path=self.bindings_path,
            center_db=self.center,
            mode=mode,
        )
        return run_delivery_cli(
            "send-quota",
            "--quota-db",
            str(self.local),
            "--bindings",
            str(self.bindings_path),
            "--binding-id",
            binding_id,
            *ssh_cli_args(self.tmp, script),
            "--format",
            "json",
        )

    def _center_ids(self) -> list[str]:
        return [item.sample_id for item in quota.iter_observations(self.center)]

    def _outbox_identities(self) -> list[tuple]:
        conn = sqlite3.connect(self.local)
        try:
            return [
                (row[0], row[1], row[2], row[3])
                for row in conn.execute(
                    "SELECT sample.machine, sample.provider, sample.account, outbox.delivered_at "
                    "FROM delivery_quota_outbox AS outbox "
                    "JOIN quota_samples AS sample ON sample.sample_id = outbox.sample_id "
                    "ORDER BY sample.machine, sample.provider, sample.account"
                )
            ]
        finally:
            conn.close()

    def test_each_binding_attempts_only_its_own_interleaved_samples(self) -> None:
        delivery_json(self._send("alpha-agy"))
        self.assertEqual(self._center_ids(), ["alpha-agy-1"])
        self.assertEqual(
            [(row[0], row[1], row[2]) for row in self._outbox_identities()],
            [("alpha", "agy", "account-a@example.test")],
        )

        delivery_json(self._send("alpha-codex"))
        self.assertEqual(sorted(self._center_ids()), ["alpha-agy-1", "alpha-codex-1"])

        delivery_json(self._send("beta-agy"))
        self.assertEqual(
            sorted(self._center_ids()), ["alpha-agy-1", "alpha-codex-1", "beta-agy-1"]
        )

        delivery_json(self._send("beta-codex"))
        self.assertEqual(
            sorted(self._center_ids()),
            ["alpha-agy-1", "alpha-codex-1", "beta-agy-1", "beta-codex-1"],
        )
        self.assertEqual(len(self._outbox_identities()), 4)
        self.assertTrue(all(row[3] for row in self._outbox_identities()))

    def test_a_failed_binding_does_not_attempt_or_block_another_provider(self) -> None:
        failed = self._send("alpha-agy", mode="hostkey")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self._center_ids(), [])
        conn = sqlite3.connect(self.local)
        try:
            rows = conn.execute(
                "SELECT outbox.sample_id, outbox.delivered_at, outbox.attempts "
                "FROM delivery_quota_outbox AS outbox "
                "JOIN quota_samples AS sample ON sample.sample_id = outbox.sample_id "
                "WHERE sample.machine = 'alpha' AND sample.provider = 'agy'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0][1])
        self.assertEqual(rows[0][2], 1)

        delivery_json(self._send("alpha-codex"))
        self.assertEqual(self._center_ids(), ["alpha-codex-1"])

        delivery_json(self._send("alpha-agy"))
        self.assertEqual(sorted(self._center_ids()), ["alpha-agy-1", "alpha-codex-1"])


if __name__ == "__main__":
    unittest.main()
