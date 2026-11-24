"""Forced-command receiver checks: identity, bounds, idempotence."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from telemetre import db
from telemetre.delivery_bindings import load_bindings
from telemetre.delivery_quota import _encode_batch
from telemetre.delivery_receive import DeliveryReceiveError, receive_bytes
from telemetre.protocol import encode_batch
from telemetre.quota import Observation, WindowObservation
from tests.test_delivery_support import write_bindings


def _usage_payload(extra: dict | None = None) -> bytes:
    fact = {
        "conversation_id": "conv-1",
        "step_index": 0,
        "event_time": "2026-11-02T00:00:00Z",
        "model": "gemini-3-pro",
        "input_tokens": 10,
        "cache_read_tokens": 1,
        "output_tokens": 2,
    }
    if extra:
        fact.update(extra)
    return encode_batch([fact], None, "test-collector")


def _quota_payload(machine: str, account: str, provider: str = "agy") -> bytes:
    observation = Observation(
        provider=provider,
        account=account,
        machine=machine,
        sampled_at="2026-11-02T00:00:00Z",
        status="success",
        windows=(
            WindowObservation(
                group="Gemini",
                bucket_id="five-hour",
                window="5h",
                window_minutes=300,
                remaining_value=0.5,
                remaining_unit="fraction",
                remaining_display=50.0,
                reset_at="2026-11-02T05:00:00Z",
            ),
        ),
        sample_id="sample-1",
    )
    return _encode_batch([observation.to_dict()])


class ReceiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.center = self.tmp / "center.sqlite3"
        self.bindings_path = write_bindings(
            self.tmp / "bindings.json",
            {
                "alpha-usage": {
                    "machine": "alpha",
                    "account": "acct@example.test",
                    "kind": "agy_usage",
                },
                "alpha-quota": {
                    "machine": "alpha",
                    "account": "acct@example.test",
                    "kind": "quota_sample",
                    "provider": "agy",
                },
            },
        )
        self.bindings = load_bindings(self.bindings_path)

    def test_identity_comes_from_the_binding_not_payload_fields(self) -> None:
        raw = _usage_payload(
            {
                "machine": "beta",
                "account": "evil@example.test",
                "content": "SYNTHETIC-CONVERSATION-BODY",
            }
        )
        ack = receive_bytes(
            self.center,
            self.bindings,
            "alpha-usage",
            raw,
            max_bytes=64 * 1024,
        )
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["machine"], "alpha")
        self.assertEqual(ack["account"], "acct@example.test")
        self.assertEqual(ack["stored"], 1)
        self.assertEqual(ack["binding_id"], "alpha-usage")

        conn = db.connect(self.center)
        try:
            event = conn.execute("SELECT * FROM events").fetchone()
            self.assertEqual((event["machine"], event["account"]), ("alpha", "acct@example.test"))
            self.assertNotIn(b"SYNTHETIC-CONVERSATION-BODY", self.center.read_bytes())
            self.assertNotIn(b"evil@example.test", self.center.read_bytes())
        finally:
            conn.close()

    def test_retry_after_commit_is_idempotent_and_bounded_input_is_refused(self) -> None:
        raw = _usage_payload()
        first = receive_bytes(self.center, self.bindings, "alpha-usage", raw)
        second = receive_bytes(self.center, self.bindings, "alpha-usage", raw)
        self.assertEqual((first["stored"], first["duplicates"]), (1, 0))
        self.assertEqual((second["stored"], second["duplicates"]), (0, 1))

        before = self.center.stat().st_size
        with self.assertRaises(DeliveryReceiveError):
            receive_bytes(
                self.center,
                self.bindings,
                "alpha-usage",
                raw + b"x",
                max_bytes=len(raw),
            )
        self.assertEqual(self.center.stat().st_size, before)
        conn = db.connect(self.center)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        finally:
            conn.close()

    def test_quota_batch_must_match_the_bound_machine_provider_and_account(self) -> None:
        wrong_machine = _quota_payload("beta", "acct@example.test")
        with self.assertRaises(DeliveryReceiveError):
            receive_bytes(
                self.center,
                self.bindings,
                "alpha-quota",
                wrong_machine,
            )

        wrong_provider = _quota_payload("alpha", "acct@example.test", provider="codex")
        with self.assertRaises(DeliveryReceiveError):
            receive_bytes(
                self.center,
                self.bindings,
                "alpha-quota",
                wrong_provider,
            )

        ack = receive_bytes(
            self.center,
            self.bindings,
            "alpha-quota",
            _quota_payload("alpha", "acct@example.test"),
        )
        self.assertEqual(ack["accepted"], 1)
        self.assertEqual(ack["stored"], 1)
        self.assertEqual(ack["provider"], "agy")
        conn = sqlite3.connect(self.center)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM quota_samples").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_unknown_binding_and_malformed_json_are_refused_without_side_effects(self) -> None:
        with self.assertRaises(Exception):
            receive_bytes(self.center, self.bindings, "missing", _usage_payload())
        secret = b'{"protocol":1,"secret":"SYNTHETIC-RAW-BODY"}'
        with self.assertRaises(Exception) as raised:
            receive_bytes(self.center, self.bindings, "alpha-usage", secret)
        self.assertNotIn("SYNTHETIC-RAW-BODY", str(raised.exception))
        self.assertFalse(self.center.exists())

    def _quota_raw(self) -> dict:
        observation = Observation(
            provider="agy",
            account="acct@example.test",
            machine="alpha",
            sampled_at="2026-11-02T00:00:00Z",
            status="success",
            windows=(
                WindowObservation(
                    group="Gemini",
                    bucket_id="five-hour",
                    window="5h",
                    window_minutes=300,
                    remaining_value=0.25,
                    remaining_unit="fraction",
                    remaining_display=25.0,
                    reset_raw="2026-11-02T05:00:00Z",
                    reset_at="2026-11-02T05:00:00Z",
                ),
            ),
            sample_id="sample-1",
        )
        return observation.to_dict()

    def test_quota_window_fields_are_validated_and_normalized_at_the_trust_boundary(self) -> None:
        raw = self._quota_raw()
        raw["windows"][0]["remaining"]["display_percent"] = 99.0
        with self.assertRaises(DeliveryReceiveError):
            receive_bytes(
                self.center,
                self.bindings,
                "alpha-quota",
                _encode_batch([raw]),
            )

        raw = self._quota_raw()
        raw["windows"][0]["reset"]["raw"] = {"nested": "SYNTHETIC-NESTED"}
        with self.assertRaises(DeliveryReceiveError):
            receive_bytes(
                self.center,
                self.bindings,
                "alpha-quota",
                _encode_batch([raw]),
            )

        raw = self._quota_raw()
        raw["windows"][0]["anomalies"] = ["SYNTHETIC-ANOMALY"]
        with self.assertRaises(DeliveryReceiveError):
            receive_bytes(
                self.center,
                self.bindings,
                "alpha-quota",
                _encode_batch([raw]),
            )

        raw = self._quota_raw()
        raw["windows"][0]["remaining"]["display_percent"] = None
        raw["windows"][0]["remaining"]["unknown"] = False
        ack = receive_bytes(
            self.center,
            self.bindings,
            "alpha-quota",
            _encode_batch([raw]),
        )
        self.assertEqual(ack["stored"], 1)
        conn = sqlite3.connect(self.center)
        try:
            stored = conn.execute(
                "SELECT remaining_display FROM quota_windows"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(stored, 25.0)

    def test_quota_failure_details_are_safe_and_batches_are_atomic(self) -> None:
        failure = Observation(
            provider="agy",
            account="acct@example.test",
            machine="alpha",
            sampled_at="2026-11-02T00:00:00Z",
            status="failure",
            error_kind="provider_error",
            error_detail="SYNTHETIC PRIVATE PROVIDER OUTPUT",
            sample_id="sample-failure",
        ).to_dict()
        ack = receive_bytes(
            self.center,
            self.bindings,
            "alpha-quota",
            _encode_batch([failure]),
        )
        self.assertEqual(ack["stored"], 1)
        conn = sqlite3.connect(self.center)
        try:
            detail = conn.execute(
                "SELECT error_detail FROM quota_samples WHERE sample_id = 'sample-failure'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn("SYNTHETIC PRIVATE", detail or "")

        unsupported_kind = dict(failure)
        unsupported_kind["sample_id"] = "sample-bad-kind"
        unsupported_kind["error"] = {"kind": "raw_exception", "detail": "x"}
        with self.assertRaises(DeliveryReceiveError):
            receive_bytes(
                self.center,
                self.bindings,
                "alpha-quota",
                _encode_batch([unsupported_kind]),
            )

        valid = self._quota_raw()
        valid["sample_id"] = "sample-valid"
        second = self._quota_raw()
        second["sample_id"] = "sample-second"
        with self.assertRaises(DeliveryReceiveError):
            receive_bytes(
                self.center,
                self.bindings,
                "alpha-quota",
                _encode_batch([valid, second]),
            )
        conn = sqlite3.connect(self.center)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM quota_samples WHERE sample_id IN "
                    "('sample-valid', 'sample-second')"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
