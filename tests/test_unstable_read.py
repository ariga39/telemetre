"""File-progress safety when a transcript changes during a read retry."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetre.collector import collect
from tests.support import model_step, write_transcript


class UnstableReadTest(unittest.TestCase):
    def test_changed_during_retry_does_not_advance_file_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            path = write_transcript(
                root,
                "conv-unstable",
                [
                    model_step(
                        0,
                        "2026-11-13T00:00:00Z",
                        input_tokens=1,
                        cache_read_tokens=0,
                        output_tokens=0,
                    )
                ],
            )
            appended = [
                model_step(
                    index,
                    f"2026-11-13T00:0{index}:00Z",
                    input_tokens=index + 1,
                    cache_read_tokens=0,
                    output_tokens=0,
                )
                for index in (1, 2)
            ]
            original_read = Path.read_bytes
            calls = {"count": 0}

            def flapping_read(self: Path) -> bytes:
                data = original_read(self)
                if calls["count"] < 2:
                    with self.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(appended[calls["count"]], separators=(",", ":"))
                            + "\n"
                        )
                calls["count"] += 1
                return data

            client = base / "client.sqlite"
            with mock.patch.object(Path, "read_bytes", new=flapping_read):
                first = collect(client, machine="alpha", account="acct", roots=[root])

            # The read never became stable.  No file signature may be stored,
            # otherwise the appended bytes would be skipped on the next run.
            conn = sqlite3.connect(client)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_state").fetchone()[0], 0)
            finally:
                conn.close()
            self.assertEqual(first["files_read"], 1)

            second = collect(client, machine="alpha", account="acct", roots=[root])
            self.assertEqual(second["files_read"], 1)
            conn = sqlite3.connect(client)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_state").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0], 3)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
