"""Bounded receive, whole-batch validation and bound-identity behaviour."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetre.cli import main
from tests.support import cli_json, model_step, run_cli, write_transcript


class ReceiveBoundsTest(unittest.TestCase):
    def _spool_three(self, base: Path) -> Path:
        root = base / "root"
        write_transcript(
            root,
            "conv-bounds",
            [
                model_step(
                    index,
                    f"2026-11-12T00:0{index}:00Z",
                    input_tokens=index + 1,
                    cache_read_tokens=0,
                    output_tokens=0,
                )
                for index in range(3)
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

    def _receive(self, center: Path, batch: bytes, *extra: str):
        return run_cli(
            "receive",
            "--center-db",
            str(center),
            "--machine",
            "alpha",
            "--account",
            "acct",
            *extra,
            stdin=batch,
        )

    def test_row_and_byte_limits_reject_whole_batch_without_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._spool_three(base)
            batch = run_cli("pending", "--client-db", str(client)).stdout
            self.assertEqual(json.loads(batch)["facts"].__len__(), 3)

            center = base / "center.sqlite"
            too_many = self._receive(center, batch, "--max-rows", "2")
            self.assertEqual(too_many.returncode, 2)
            self.assertEqual(json.loads(too_many.stdout)["error"], "invalid batch")
            empty = cli_json(
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
            self.assertEqual(empty["accounts"], {})

            too_big = self._receive(center, batch, "--max-bytes", "100")
            self.assertEqual(too_big.returncode, 2)
            empty = cli_json(
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
            self.assertEqual(empty["accounts"], {})

            ok = cli_json(self._receive(center, batch))
            self.assertEqual(ok["stored"], 3)

    def test_one_invalid_fact_rejects_the_whole_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._spool_three(base)
            batch = json.loads(run_cli("pending", "--client-db", str(client)).stdout)
            batch["facts"][2]["input_tokens"] = -1
            center = base / "center.sqlite"
            result = self._receive(center, json.dumps(batch).encode())
            self.assertEqual(result.returncode, 2)
            empty = cli_json(
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
            self.assertEqual(empty["accounts"], {})

    def test_invalid_event_and_scan_timestamps_are_invalid_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._spool_three(base)
            valid = json.loads(run_cli("pending", "--client-db", str(client)).stdout)
            bad_event = json.loads(json.dumps(valid))
            bad_event["facts"][0]["event_time"] = "not-a-time"
            bad_event_range = json.loads(json.dumps(valid))
            bad_event_range["facts"][0]["event_time"] = "0001-01-01T00:00:00+01:00"
            bad_scan = json.loads(json.dumps(valid))
            bad_scan["scan"]["completed_at"] = "not-a-time"
            center = base / "center.sqlite"
            for label, batch in (
                ("event", bad_event),
                ("event-range", bad_event_range),
                ("scan", bad_scan),
            ):
                with self.subTest(label=label):
                    result = self._receive(center, json.dumps(batch).encode())
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(json.loads(result.stdout)["error"], "invalid batch")
                    self.assertNotIn(b"Traceback", result.stdout + result.stderr)
            empty = cli_json(
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
            self.assertEqual(empty["accounts"], {})

    def test_operational_database_failure_stays_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._spool_three(base)
            batch = run_cli("pending", "--client-db", str(client)).stdout
            center_directory = base / "center-is-a-directory"
            center_directory.mkdir()
            result = self._receive(center_directory, batch)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["error"], "receive failed")
            self.assertNotIn(b"Traceback", result.stdout + result.stderr)

    def test_invalid_limits_fail_before_stdin_read(self) -> None:
        class NoReadStdin:
            class Buffer:
                def read(self, *args, **kwargs):
                    raise AssertionError("stdin was read before limit validation")

            buffer = Buffer()

        with tempfile.TemporaryDirectory() as tmp:
            center = Path(tmp) / "center.sqlite"
            for flag, value in (
                ("--max-bytes", "-2"),
                ("--max-bytes", "0"),
                ("--max-rows", "-1"),
                ("--max-rows", "0"),
            ):
                with self.subTest(flag=flag, value=value):
                    output = io.StringIO()
                    with mock.patch("sys.stdin", NoReadStdin()), contextlib.redirect_stdout(output):
                        rc = main(
                            [
                                "receive",
                                "--center-db",
                                str(center),
                                "--machine",
                                "alpha",
                                "--account",
                                "acct",
                                flag,
                                value,
                            ]
                        )
                    self.assertEqual(rc, 2)
                    self.assertEqual(json.loads(output.getvalue())["error"], "invalid batch")

    def test_json_shape_and_sqlite_overflow_are_generic_receive_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._spool_three(base)
            valid = json.loads(run_cli("pending", "--client-db", str(client)).stdout)
            huge_fact_count = json.loads(json.dumps(valid))
            huge_fact_count["facts"][0]["input_tokens"] = 2**63
            huge_step = json.loads(json.dumps(valid))
            huge_step["facts"][0]["step_index"] = 2**63
            huge_scan = json.loads(json.dumps(valid))
            huge_scan["scan"]["files_seen"] = 2**63
            cases = [
                b"{",
                b"[]",
                b'{"protocol":1,"kind":"agy_usage","facts":"not-a-list"}',
                json.dumps(huge_fact_count).encode(),
                json.dumps(huge_step).encode(),
                json.dumps(huge_scan).encode(),
            ]
            center = base / "center.sqlite"
            for raw in cases:
                with self.subTest(raw=raw[:80]):
                    result = self._receive(center, raw)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(
                        json.loads(result.stdout)["error"],
                        "invalid batch",
                    )
                    combined = result.stdout + result.stderr
                    self.assertNotIn(b"Traceback", combined)
                    self.assertNotIn(b"Overflow", combined)
            empty = cli_json(
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
            self.assertEqual(empty["accounts"], {})

    def test_unpaired_surrogate_text_is_generic_failure_without_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._spool_three(base)
            valid = json.loads(run_cli("pending", "--client-db", str(client)).stdout)
            cases = []
            model_bad = json.loads(json.dumps(valid))
            model_bad["facts"][0]["model"] = "\ud800"
            cases.append(("model", model_bad))
            version_bad = json.loads(json.dumps(valid))
            version_bad["collector_version"] = "\ud800"
            cases.append(("collector_version", version_bad))
            scan_version_bad = json.loads(json.dumps(valid))
            scan_version_bad["scan"]["collector_version"] = "\ud800"
            cases.append(("scan.collector_version", scan_version_bad))

            center = base / "center.sqlite"
            for label, batch in cases:
                with self.subTest(label=label):
                    result = self._receive(center, json.dumps(batch).encode())
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(json.loads(result.stdout)["error"], "invalid batch")
                    self.assertNotIn(b"Traceback", result.stdout + result.stderr)
            empty = cli_json(
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
            self.assertEqual(empty["accounts"], {})

    def test_payload_identity_claim_is_ignored_and_kind_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = self._spool_three(base)
            batch = json.loads(run_cli("pending", "--client-db", str(client)).stdout)
            batch["machine"] = "spoofed"
            batch["account"] = "someone-else"
            center = base / "center.sqlite"
            ack = cli_json(self._receive(center, json.dumps(batch).encode()))
            self.assertEqual(ack["machine"], "alpha")
            self.assertEqual(ack["account"], "acct")
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
            self.assertEqual(list(report["accounts"]), ["acct"])
            self.assertEqual(len(report["accounts"]["acct"]["machines"][0]["models"]), 1)

            wrong_kind = self._receive(
                base / "other.sqlite",
                json.dumps(batch).encode(),
                "--kind",
                "quota",
            )
            self.assertEqual(wrong_kind.returncode, 2)


if __name__ == "__main__":
    unittest.main()
