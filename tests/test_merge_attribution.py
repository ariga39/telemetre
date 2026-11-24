"""Cross-machine merge, attribution, conflict isolation and rewind evidence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support import cli_json, model_step, run_cli, write_transcript


def _collect(root: Path, client: Path, machine: str) -> dict:
    return cli_json(
        run_cli(
            "collect",
            "--client-db",
            str(client),
            "--machine",
            machine,
            "--account",
            "acct",
            "--root",
            str(root),
            "--format",
            "json",
        )
    )


def _replay(client: Path, center: Path, machine: str) -> dict:
    return cli_json(
        run_cli(
            "replay",
            "--client-db",
            str(client),
            "--center-db",
            str(center),
            "--machine",
            machine,
            "--account",
            "acct",
            "--format",
            "json",
        )
    )


def _report(center: Path) -> dict:
    return cli_json(
        run_cli(
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
    )


class MergeAttributionTest(unittest.TestCase):
    def test_copied_prefix_on_two_machines_is_unattributed_and_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            center = base / "center.sqlite"
            for machine in ("alpha", "beta"):
                root = base / machine
                write_transcript(
                    root,
                    "conv-copied",
                    [
                        model_step(
                            0,
                            "2026-11-08T00:00:00Z",
                            input_tokens=10,
                            cache_read_tokens=1,
                            output_tokens=2,
                        )
                    ],
                )
                client = base / f"{machine}.sqlite"
                _collect(root, client, machine)
                _replay(client, center, machine)

            account = _report(center)["accounts"]["acct"]
            self.assertEqual(account["account_total"], {
                "input_tokens": 10,
                "cache_read_tokens": 1,
                "output_tokens": 2,
                "model_steps": 1,
            })
            self.assertEqual(account["machines"], [])
            self.assertEqual(len(account["unattributed"]), 1)
            row = account["unattributed"][0]
            self.assertEqual(row["model"], "gemini-3-pro")
            self.assertEqual(row["model_steps"], 1)
            self.assertEqual(row["sources"], ["alpha", "beta"])
            self.assertEqual(account["conflicts"], [])

    def test_conflicting_counts_are_isolated_with_two_structured_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            center = base / "center.sqlite"
            for machine, input_tokens in (("alpha", 10), ("beta", 11)):
                root = base / machine
                write_transcript(
                    root,
                    "conv-conflict",
                    [
                        model_step(
                            0,
                            "2026-11-09T00:00:00Z",
                            input_tokens=input_tokens,
                            cache_read_tokens=2,
                            output_tokens=3,
                        )
                    ],
                )
                client = base / f"{machine}.sqlite"
                _collect(root, client, machine)
                _replay(client, center, machine)

            account = _report(center)["accounts"]["acct"]
            self.assertEqual(account["account_total"], {
                "input_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": 0,
                "model_steps": 0,
            })
            self.assertEqual(account["machines"], [])
            self.assertEqual(account["unattributed"], [])
            self.assertEqual(len(account["conflicts"]), 1)
            conflict = account["conflicts"][0]
            self.assertEqual(conflict["conversation_id"], "conv-conflict")
            self.assertEqual(conflict["step_index"], 0)
            self.assertEqual(len(conflict["observations"]), 2)
            self.assertEqual(
                sorted((row["machine"], row["input_tokens"]) for row in conflict["observations"]),
                [("alpha", 10), ("beta", 11)],
            )

    def test_only_previously_missing_fields_merge_and_account_total_includes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            center = base / "center.sqlite"
            observations = {
                "alpha": dict(
                    input_tokens=10,
                    output_tokens=3,
                    include=("input_tokens", "output_tokens"),
                ),
                "beta": dict(
                    input_tokens=10,
                    cache_read_tokens=4,
                    output_tokens=3,
                    include=("input_tokens", "cache_read_tokens", "output_tokens"),
                ),
            }
            for machine, fields in observations.items():
                root = base / machine
                write_transcript(
                    root,
                    "conv-merge",
                    [model_step(0, "2026-11-10T00:00:00Z", **fields)],
                )
                client = base / f"{machine}.sqlite"
                _collect(root, client, machine)
                _replay(client, center, machine)

            account = _report(center)["accounts"]["acct"]
            self.assertEqual(account["account_total"], {
                "input_tokens": 10,
                "cache_read_tokens": 4,
                "output_tokens": 3,
                "model_steps": 1,
            })
            self.assertEqual(account["conflicts"], [])
            self.assertEqual(len(account["unattributed"]), 1)
            self.assertEqual(account["unattributed"][0]["sources"], ["alpha", "beta"])

    def test_same_key_changed_model_is_conflict_even_when_counts_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            center = base / "center.sqlite"
            for machine, model in (("alpha", "model-a"), ("beta", "model-b")):
                root = base / machine
                write_transcript(
                    root,
                    "conv-model-change",
                    [
                        model_step(
                            0,
                            "2026-11-16T00:00:00Z",
                            model=model,
                            input_tokens=10,
                            cache_read_tokens=1,
                            output_tokens=2,
                        )
                    ],
                )
                client = base / f"{machine}.sqlite"
                _collect(root, client, machine)
                _replay(client, center, machine)

            account = _report(center)["accounts"]["acct"]
            self.assertEqual(account["account_total"]["model_steps"], 0)
            self.assertEqual(account["machines"], [])
            self.assertEqual(account["unattributed"], [])
            self.assertEqual(len(account["conflicts"]), 1)
            conflict = account["conflicts"][0]
            self.assertEqual(
                sorted(row["model"] for row in conflict["observations"]),
                ["model-a", "model-b"],
            )

    def test_same_key_changed_event_time_is_conflict_in_both_months(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            center = base / "center.sqlite"
            for machine, event_time in (
                ("alpha", "2026-01-15T00:00:00Z"),
                ("beta", "2026-02-15T00:00:00Z"),
            ):
                root = base / machine
                write_transcript(
                    root,
                    "conv-time-change",
                    [
                        model_step(
                            0,
                            event_time,
                            input_tokens=10,
                            cache_read_tokens=1,
                            output_tokens=2,
                        )
                    ],
                )
                client = base / f"{machine}.sqlite"
                _collect(root, client, machine)
                _replay(client, center, machine)

            january = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-01",
                    "--tz",
                    "UTC",
                    "--format",
                    "json",
                )
            )["accounts"]["acct"]
            self.assertEqual(january["account_total"]["model_steps"], 0)
            self.assertEqual(len(january["conflicts"]), 1)
            self.assertEqual(
                sorted(row["event_time"] for row in january["conflicts"][0]["observations"]),
                ["2026-01-15T00:00:00Z", "2026-02-15T00:00:00Z"],
            )

            february = cli_json(
                run_cli(
                    "report",
                    "--center-db",
                    str(center),
                    "--month",
                    "2026-02",
                    "--tz",
                    "UTC",
                    "--format",
                    "json",
                )
            )["accounts"]["acct"]
            self.assertEqual(february["account_total"]["model_steps"], 0)
            self.assertEqual(len(february["conflicts"]), 1)
            self.assertEqual(
                sorted(row["event_time"] for row in february["conflicts"][0]["observations"]),
                ["2026-01-15T00:00:00Z", "2026-02-15T00:00:00Z"],
            )

    def test_same_machine_reused_step_index_with_different_counts_is_conflict(self) -> None:
        """Fork/rewind handling is deliberately conservative, not claimed exact.

        The sampled transcript shape has no parent reference we can verify.
        Reusing (conversation, step_index) with different counts therefore keeps
        both observations and isolates them as a conflict.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            center = base / "center.sqlite"
            root = base / "root"
            path = write_transcript(
                root,
                "conv-rewind",
                [
                    model_step(
                        0,
                        "2026-11-11T00:00:00Z",
                        input_tokens=10,
                        cache_read_tokens=1,
                        output_tokens=2,
                    )
                ],
            )
            with path.open("a", encoding="utf-8") as handle:
                import json

                handle.write(
                    json.dumps(
                        model_step(
                            0,
                            "2026-11-11T00:00:00Z",
                            input_tokens=11,
                            cache_read_tokens=1,
                            output_tokens=2,
                        ),
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            client = base / "client.sqlite"
            _collect(root, client, "alpha")
            _replay(client, center, "alpha")

            account = _report(center)["accounts"]["acct"]
            self.assertEqual(account["account_total"]["model_steps"], 0)
            self.assertEqual(len(account["conflicts"]), 1)
            self.assertEqual(
                sorted(row["input_tokens"] for row in account["conflicts"][0]["observations"]),
                [10, 11],
            )
            self.assertEqual(account["coverage"]["reasoning"], "unknown")


if __name__ == "__main__":
    unittest.main()
