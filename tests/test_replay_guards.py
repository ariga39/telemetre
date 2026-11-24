"""Handoff guards requested in early lead review.

These tests exercise the client spool, the replay seam and the receive boundary
without any real agy data or network access.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from telemetre.protocol import encode_batch
from telemetre.replay import (
    LocalCenterTransport,
    ReplayError,
    build_next_batch,
    replay_pending,
)
from telemetre.protocol import IngestContext
from tests.support import cli_json, model_step, run_cli, write_transcript


class _BadAckTransport:
    def __init__(self, ack):
        self.ack = ack
        self.calls = 0

    def send(self, batch: bytes):
        self.calls += 1
        return self.ack


class _MatchingAckTransport:
    """A well-formed ACK derived from the batch, for the happy-path control."""

    def __init__(self):
        self.calls = 0

    def send(self, batch: bytes):
        self.calls += 1
        payload = json.loads(batch)
        return {
            "ok": True,
            "accepted": len(payload["facts"]),
            "kind": "agy_usage",
            "machine": "alpha",
            "account": "acct",
            "stored": len(payload["facts"]),
            "duplicates": 0,
            "conflicts": 0,
        }


class ReplayGuardTest(unittest.TestCase):
    def _collect_one(self, base: Path, model: str = "gemini-3-pro") -> Path:
        root = base / "root"
        write_transcript(
            root,
            "conv-guard",
            [
                model_step(
                    0,
                    "2026-11-02T00:00:00Z",
                    model=model,
                    input_tokens=10,
                    cache_read_tokens=1,
                    output_tokens=2,
                )
            ],
        )
        client = base / "client.sqlite"
        cli_json(
            run_cli(
                "collect",
                "--client-db",
                str(client),
                "--machine",
                "alpha",
                "--account",
                "acct",
                "--root",
                str(root),
                "--format",
                "json",
            )
        )
        return client

    def test_malformed_or_negative_ack_keeps_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._collect_one(base)
            context = IngestContext("alpha", "acct")
            bad_acks = [
                "not-an-object",
                {"ok": False},
                {"ok": True, "duplicates": -1},
                {"ok": True, "stored": True},
            ]
            for ack in bad_acks:
                with self.subTest(ack=ack):
                    with self.assertRaises(ReplayError):
                        replay_pending(
                            client,
                            _BadAckTransport(ack),
                            max_rows=10,
                            max_bytes=64 * 1024,
                        )
                    batch = build_next_batch(client, max_rows=10, max_bytes=64 * 1024)
                    self.assertEqual(batch.fact_count, 1)
                    conn = sqlite3.connect(client)
                    try:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0], 1)
                    finally:
                        conn.close()

    def test_ack_must_match_batch_size_and_bound_identity_before_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._collect_one(base)
            bad_acks = [
                {"ok": True, "accepted": 0, "stored": 0, "duplicates": 0,
                 "kind": "agy_usage", "machine": "alpha", "account": "acct"},
                {"ok": True, "accepted": 1, "stored": 1, "duplicates": 0,
                 "kind": "agy_usage", "machine": "beta", "account": "acct"},
                {"ok": True, "accepted": 1, "stored": 1, "duplicates": 0,
                 "kind": "agy_usage", "machine": "alpha", "account": "other"},
                {"ok": True, "accepted": 1, "stored": 1, "duplicates": 0,
                 "kind": "quota", "machine": "alpha", "account": "acct"},
                {"ok": True, "accepted": 1, "stored": 1, "duplicates": 0,
                 "kind": "agy_usage", "machine": "alpha"},
                {"ok": True, "stored": 1, "duplicates": 0,
                 "kind": "agy_usage", "machine": "alpha", "account": "acct"},
                {"ok": True, "accepted": 1, "stored": 1, "duplicates": 0,
                 "kind": "agy_usage", "machine": "alpha", "account": "acct",
                 "conflicts": None},
            ]
            for ack in bad_acks:
                with self.subTest(ack=ack):
                    with self.assertRaises(ReplayError):
                        replay_pending(
                            client,
                            _BadAckTransport(ack),
                            max_rows=10,
                            max_bytes=64 * 1024,
                        )
                    self.assertEqual(build_next_batch(client, max_rows=10).fact_count, 1)

            replay_pending(
                client,
                _MatchingAckTransport(),
                max_rows=10,
                max_bytes=64 * 1024,
            )
            self.assertEqual(build_next_batch(client, max_rows=10).fact_count, 0)

    def test_public_replay_rejects_inconsistent_counters_then_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._collect_one(base)
            identity = {
                "ok": True,
                "accepted": 1,
                "kind": "agy_usage",
                "machine": "alpha",
                "account": "acct",
            }
            contradictory = [
                {**identity, "stored": 0, "duplicates": 0},
                {**identity, "stored": 1, "duplicates": 1},
                {**identity, "stored": 1},
                {**identity, "duplicates": 0},
            ]
            for ack in contradictory:
                with self.subTest(ack=ack):
                    with self.assertRaises(ReplayError):
                        replay_pending(
                            client,
                            _BadAckTransport(ack),
                            max_rows=10,
                            max_bytes=64 * 1024,
                        )
                    self.assertEqual(build_next_batch(client, max_rows=10).fact_count, 1)

            center = base / "center.sqlite"
            replayed = cli_json(
                run_cli(
                    "replay",
                    "--client-db",
                    str(client),
                    "--center-db",
                    str(center),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(replayed["deleted_pending"], 1)
            report = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-11",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(report["accounts"]["acct"]["account_total"]["model_steps"], 1)

            # Status-only ACK semantics: accepted/stored/duplicates are all 0.
            replay_pending(
                client,
                _MatchingAckTransport(),
                max_rows=10,
                max_bytes=64 * 1024,
            )

    def test_single_fact_over_cap_fails_clearly_and_retains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._collect_one(base, model="m" * 4000)
            batch = build_next_batch(client)
            payload = json.loads(batch.payload)
            scan_only = encode_batch([], payload["scan"], payload.get("collector_version"))
            cap = len(scan_only) + 8

            with self.assertRaises(ReplayError) as raised:
                replay_pending(
                    client,
                    LocalCenterTransport(base / "center.sqlite", IngestContext("alpha", "acct")),
                    max_bytes=cap,
                )
            message = str(raised.exception)
            self.assertIn("single fact", message)
            self.assertIn("retain", message)
            self.assertEqual(build_next_batch(client).fact_count, 1)

            cli = run_cli("pending", "--client-db", str(client), "--max-bytes", str(cap))
            self.assertNotEqual(cli.returncode, 0)
            self.assertIn(b"single fact", cli.stderr)

    def test_batch_and_report_do_not_carry_local_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._collect_one(base)
            pending = run_cli("pending", "--client-db", str(client))
            self.assertEqual(pending.returncode, 0, pending.stderr.decode())
            self.assertNotIn(str(base).encode(), pending.stdout)
            batch = json.loads(pending.stdout)
            self.assertNotIn("roots", batch["scan"])

            center = base / "center.sqlite"
            cli_json(
                run_cli(
                    "replay",
                    "--client-db",
                    str(client),
                    "--center-db",
                    str(center),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    "--format",
                    "json",
                )
            )
            report = run_cli(
                "report",
                "--center-db",
                str(center),
                "--month",
                "2026-11",
                "--tz",
                "UTC",
                "--format",
                "json",
            )
            self.assertEqual(report.returncode, 0, report.stderr.decode())
            self.assertNotIn(str(base).encode(), report.stdout)

    def test_only_done_planner_response_is_a_model_event_and_missing_counts_are_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            write_transcript(
                root,
                "conv-shape",
                [
                    model_step(
                        0,
                        "2026-11-03T00:00:00Z",
                        input_tokens=10,
                        cache_read_tokens=1,
                        output_tokens=2,
                    ),
                    {
                        "step_index": 1,
                        "created_at": "2026-11-03T00:01:00Z",
                        "type": "PLANNER_RESPONSE",
                        "status": "ERROR",
                        "model": "gemini-3-pro",
                        "input_tokens": 100,
                        "cache_read_tokens": 10,
                        "output_tokens": 20,
                    },
                    {
                        "step_index": 2,
                        "created_at": "2026-11-03T00:02:00Z",
                        "type": "TOOL_CALL",
                        "status": "DONE",
                        "model": "gemini-3-pro",
                        "input_tokens": 200,
                        "cache_read_tokens": 20,
                        "output_tokens": 40,
                    },
                    {
                        "step_index": 3,
                        "created_at": "2026-11-03T00:03:00Z",
                        "type": "USER_INPUT",
                        "status": "DONE",
                    },
                    {
                        "step_index": 4,
                        "created_at": "2026-11-03T00:04:00Z",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "model": "gemini-3-pro",
                    },
                ],
            )
            client = base / "client.sqlite"
            collected = cli_json(
                run_cli(
                    "collect",
                    "--client-db",
                    str(client),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    "--root",
                    str(root),
                    "--format",
                    "json",
                )
            )
            self.assertEqual(collected["bad_lines"], 0)
            self.assertEqual(collected["missing_count_steps"], 1)
            center = base / "center.sqlite"
            cli_json(
                run_cli(
                    "replay",
                    "--client-db",
                    str(client),
                    "--center-db",
                    str(center),
                    "--machine",
                    "alpha",
                    "--account",
                    "acct",
                    "--format",
                    "json",
                )
            )
            report = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-11",
                    "--format",
                    "json",
                )
            )
            account = report["accounts"]["acct"]
            self.assertEqual(account["account_total"]["input_tokens"], 10)
            self.assertEqual(account["account_total"]["cache_read_tokens"], 1)
            self.assertEqual(account["account_total"]["output_tokens"], 2)
            self.assertEqual(account["account_total"]["model_steps"], 2)
            self.assertEqual(account["coverage"]["missing"], {
                "input_tokens": 1,
                "cache_read_tokens": 1,
                "output_tokens": 1,
            })

    def test_receive_protocol_error_does_not_echo_raw_exception_or_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._collect_one(base)
            batch = json.loads(run_cli("pending", "--client-db", str(client)).stdout)
            batch["facts"][0]["input_tokens"] = -5
            center = base / "center.sqlite"
            result = run_cli(
                "receive",
                "--center-db",
                str(center),
                "--machine",
                "alpha",
                "--account",
                "acct",
                stdin=json.dumps(batch).encode(),
            )
            self.assertEqual(result.returncode, 2)
            output = result.stdout.decode()
            self.assertIn("invalid batch", output)
            self.assertNotIn("must be", output)
            self.assertNotIn(str(base), output)
            self.assertNotIn(str(base), result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
