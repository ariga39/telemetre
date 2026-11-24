"""End-to-end delivery paths through the normal CLIs and a fake SSH host."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telemetre import quota
from tests.support import append_raw, cli_json, model_step, run_cli, write_transcript
from tests.test_delivery_support import (
    delivery_json,
    run_delivery_cli,
    run_quota_cli,
    set_fake_mode,
    ssh_cli_args,
    write_bindings,
    write_fake_ssh,
)

ACCOUNT = "acct@example.test"
SECRET = "SYNTHETIC-CONVERSATION-BODY-MUST-NOT-LEAVE"


def _binding(machine: str, kind: str = "agy_usage", provider: str | None = None) -> dict:
    data = {"machine": machine, "account": ACCOUNT, "kind": kind}
    if provider:
        data["provider"] = provider
    return data


def _collect(base: Path, machine: str, conversation: str, rows: list[dict]) -> Path:
    root = base / f"root-{machine}"
    write_transcript(root, conversation, rows)
    client = base / f"{machine}.sqlite3"
    cli_json(
        run_cli(
            "collect",
            "--client-db",
            str(client),
            "--machine",
            machine,
            "--account",
            ACCOUNT,
            "--root",
            str(root),
            "--format",
            "json",
        )
    )
    return client


def _pending(client: Path) -> int:
    conn = sqlite3.connect(client)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
    finally:
        conn.close()


def _send(client: Path, bindings: Path, binding_id: str, directory: Path, script: Path, **kwargs):
    return run_delivery_cli(
        "send",
        "--client-db",
        str(client),
        "--bindings",
        str(bindings),
        "--binding-id",
        binding_id,
        *ssh_cli_args(directory, script, **kwargs),
        "--format",
        "json",
    )


class DeliveryEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.center = self.tmp / "center.sqlite3"
        self.bindings = write_bindings(
            self.tmp / "bindings.json",
            {
                "alpha-usage": _binding("alpha"),
                "beta-usage": _binding("beta"),
                "alpha-quota": _binding("alpha", "quota_sample", "agy"),
            },
        )

    def test_two_hosts_report_totals_without_persisting_conversation_text(self) -> None:
        alpha = _collect(
            self.tmp,
            "alpha",
            "conv-a",
            [
                model_step(
                    0,
                    "2026-11-02T00:00:00Z",
                    input_tokens=100,
                    cache_read_tokens=10,
                    output_tokens=5,
                    include=("input_tokens", "cache_read_tokens", "output_tokens"),
                )
                | {"content": SECRET, "prompt": SECRET},
                model_step(
                    1,
                    "2026-11-03T00:00:00Z",
                    input_tokens=200,
                    cache_read_tokens=20,
                    output_tokens=6,
                )
                | {"content": SECRET},
            ],
        )
        beta = _collect(
            self.tmp,
            "beta",
            "conv-b",
            [
                model_step(
                    0,
                    "2026-11-04T00:00:00Z",
                    model="gemini-3-flash",
                    input_tokens=400,
                    cache_read_tokens=40,
                    output_tokens=7,
                )
                | {"content": SECRET},
            ],
        )
        alpha_ssh = write_fake_ssh(
            self.tmp,
            name="alpha-ssh",
            binding_id="alpha-usage",
            bindings_path=self.bindings,
            center_db=self.center,
        )
        beta_ssh = write_fake_ssh(
            self.tmp,
            name="beta-ssh",
            binding_id="beta-usage",
            bindings_path=self.bindings,
            center_db=self.center,
        )
        alpha_result = delivery_json(_send(alpha, self.bindings, "alpha-usage", self.tmp, alpha_ssh))
        beta_result = delivery_json(_send(beta, self.bindings, "beta-usage", self.tmp, beta_ssh))
        self.assertEqual(alpha_result["sent_facts"], 2)
        self.assertEqual(beta_result["sent_facts"], 1)
        self.assertEqual(_pending(alpha), 0)
        self.assertEqual(_pending(beta), 0)

        report = cli_json(
            run_cli(
                "report",
                "--center-db",
                str(self.center),
                "--month",
                "2026-11",
                "--format",
                "json",
            )
        )
        account = report["accounts"][ACCOUNT]
        self.assertEqual(
            account["account_total"],
            {
                "input_tokens": 700,
                "cache_read_tokens": 70,
                "output_tokens": 18,
                "model_steps": 3,
            },
        )
        self.assertEqual([row["machine"] for row in account["machines"]], ["alpha", "beta"])
        self.assertNotIn(SECRET.encode(), self.center.read_bytes())

    def test_offline_timeout_host_key_and_lost_ack_keep_the_spool_until_retry(self) -> None:
        client = _collect(
            self.tmp,
            "alpha",
            "conv-retry",
            [
                model_step(
                    0,
                    "2026-11-02T00:00:00Z",
                    input_tokens=10,
                    cache_read_tokens=1,
                    output_tokens=2,
                )
            ],
        )
        script = write_fake_ssh(
            self.tmp,
            name="retry-ssh",
            binding_id="alpha-usage",
            bindings_path=self.bindings,
            center_db=self.center,
            mode="timeout",
        )
        timed_out = _send(client, self.bindings, "alpha-usage", self.tmp, script, timeout=0.4)
        self.assertNotEqual(timed_out.returncode, 0)
        self.assertEqual(_pending(client), 1)
        self.assertFalse(self.center.exists())

        set_fake_mode(script, "hostkey")
        host_key = _send(client, self.bindings, "alpha-usage", self.tmp, script)
        self.assertNotEqual(host_key.returncode, 0)
        self.assertEqual(_pending(client), 1)

        set_fake_mode(script, "ok")
        delivered = delivery_json(_send(client, self.bindings, "alpha-usage", self.tmp, script))
        self.assertEqual(delivered["sent_facts"], 1)
        self.assertEqual(_pending(client), 0)

        append_raw(
            self.tmp / "root-alpha" / "brain" / "conv-retry" / ".system_generated" / "logs" / "transcript.jsonl",
            json.dumps(
                model_step(
                    1,
                    "2026-11-02T01:00:00Z",
                    input_tokens=20,
                    cache_read_tokens=2,
                    output_tokens=3,
                )
            )
            + "\n",
        )
        cli_json(
            run_cli(
                "collect",
                "--client-db",
                str(client),
                "--machine",
                "alpha",
                "--account",
                ACCOUNT,
                "--root",
                str(self.tmp / "root-alpha"),
                "--format",
                "json",
            )
        )
        set_fake_mode(script, "drop-ack")
        lost = _send(client, self.bindings, "alpha-usage", self.tmp, script)
        self.assertNotEqual(lost.returncode, 0)
        self.assertEqual(_pending(client), 1)

        set_fake_mode(script, "ok")
        retried = delivery_json(_send(client, self.bindings, "alpha-usage", self.tmp, script))
        self.assertEqual(retried["duplicates"], 1)
        self.assertEqual(_pending(client), 0)
        report = cli_json(
            run_cli(
                "report",
                "--center-db",
                str(self.center),
                "--month",
                "2026-11",
                "--format",
                "json",
            )
        )
        self.assertEqual(report["accounts"][ACCOUNT]["account_total"]["model_steps"], 2)
        self.assertEqual(report["accounts"][ACCOUNT]["account_total"]["input_tokens"], 30)

    def test_concurrent_clients_converge_on_the_same_totals(self) -> None:
        clients = [
            _collect(
                self.tmp,
                machine,
                f"conv-{machine}",
                [
                    model_step(
                        0,
                        "2026-11-05T00:00:00Z",
                        input_tokens=10,
                        cache_read_tokens=1,
                        output_tokens=2,
                    )
                ],
            )
            for machine in ("alpha", "beta")
        ]
        scripts = [
            write_fake_ssh(
                self.tmp,
                name=f"{machine}-concurrent-ssh",
                binding_id=f"{machine}-usage",
                bindings_path=self.bindings,
                center_db=self.center,
            )
            for machine in ("alpha", "beta")
        ]
        results = []

        def send_one(index: int) -> None:
            machine = "alpha" if index == 0 else "beta"
            results.append(
                _send(
                    clients[index],
                    self.bindings,
                    f"{machine}-usage",
                    self.tmp,
                    scripts[index],
                )
            )

        threads = [threading.Thread(target=send_one, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(results), 2)
        for result in results:
            delivery_json(result)
        report = cli_json(
            run_cli(
                "report",
                "--center-db",
                str(self.center),
                "--month",
                "2026-11",
                "--format",
                "json",
            )
        )
        total = report["accounts"][ACCOUNT]["account_total"]
        self.assertEqual(total["model_steps"], 2)
        self.assertEqual(total["input_tokens"], 20)
        self.assertEqual([_pending(client) for client in clients], [0, 0])

    def test_center_daily_digest_covers_yesterday_and_both_providers(self) -> None:
        for provider, sample_id, unit, value, display in (
            ("agy", "daily-agy", "fraction", 0.4, 40.0),
            ("codex", "daily-codex", "percent_used", 30.0, 70.0),
        ):
            quota.ingest_observation(
                self.center,
                quota.Observation(
                    provider=provider,
                    account=ACCOUNT,
                    machine="alpha",
                    sampled_at="2026-11-05T12:00:00Z",
                    status="success",
                    windows=(
                        quota.WindowObservation(
                            group=provider,
                            bucket_id="primary",
                            window_minutes=300,
                            remaining_value=value,
                            remaining_unit=unit,
                            remaining_display=display,
                            reset_at="2026-11-05T17:00:00Z",
                        ),
                    ),
                    sample_id=sample_id,
                ),
            )
        config = self.tmp / "quota-center.json"
        config.write_text(
            json.dumps(
                {
                    "machine": "centre",
                    "timezone": "UTC",
                    "providers": [
                        {"provider": "agy", "account": ACCOUNT, "interval_seconds": 900},
                        {"provider": "codex", "account": ACCOUNT, "interval_seconds": 900},
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = run_quota_cli(
            "daily",
            "--config",
            str(config),
            "--db",
            str(self.center),
            "--now",
            "2026-11-06T01:00:00Z",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        summary = json.loads(result.stdout)
        self.assertEqual(summary["mode"], "daily")
        self.assertEqual(summary["date"], "2026-11-05")
        self.assertEqual(
            {account["provider"] for account in summary["accounts"]},
            {"agy", "codex"},
        )
        stored = quota.get_daily_summary(self.center, "2026-11-05", "UTC")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["date"], "2026-11-05")

    def test_backup_restore_and_health_cli_preserve_reports_and_detect_missing_sources(self) -> None:
        missing = run_delivery_cli(
            "check",
            "--center-db",
            str(self.center),
            "--expect-usage",
            f"alpha:{ACCOUNT}",
            "--expect-quota",
            f"alpha:agy:{ACCOUNT}",
            "--format",
            "json",
        )
        self.assertEqual(missing.returncode, 3)
        missing_payload = json.loads(missing.stdout)
        self.assertFalse(missing_payload["ok"])
        self.assertEqual(len(missing_payload["checks"]), 2)

        client = _collect(
            self.tmp,
            "alpha",
            "conv-backup",
            [
                model_step(
                    0,
                    "2026-11-06T00:00:00Z",
                    input_tokens=50,
                    cache_read_tokens=5,
                    output_tokens=5,
                )
            ],
        )
        usage_ssh = write_fake_ssh(
            self.tmp,
            name="usage-backup-ssh",
            binding_id="alpha-usage",
            bindings_path=self.bindings,
            center_db=self.center,
        )
        delivery_json(_send(client, self.bindings, "alpha-usage", self.tmp, usage_ssh))

        sampled = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        reset = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        local_quota = self.tmp / "local-quota.sqlite3"
        quota.ingest_observation(
            local_quota,
            quota.Observation(
                provider="agy",
                account=ACCOUNT,
                machine="alpha",
                sampled_at=sampled,
                status="success",
                windows=(
                    quota.WindowObservation(
                        group="Gemini",
                        bucket_id="five-hour",
                        window="5h",
                        window_minutes=300,
                        remaining_value=0.5,
                        remaining_unit="fraction",
                        remaining_display=50.0,
                        reset_at=reset,
                    ),
                ),
                sample_id="quota-backup-1",
            ),
        )
        quota_ssh = write_fake_ssh(
            self.tmp,
            name="quota-backup-ssh",
            binding_id="alpha-quota",
            bindings_path=self.bindings,
            center_db=self.center,
        )
        quota_sent = delivery_json(
            run_delivery_cli(
                "send-quota",
                "--quota-db",
                str(local_quota),
                "--bindings",
                str(self.bindings),
                "--binding-id",
                "alpha-quota",
                *ssh_cli_args(self.tmp, quota_ssh),
                "--format",
                "json",
            )
        )
        self.assertEqual(quota_sent["sent_observations"], 1)

        healthy = run_delivery_cli(
            "check",
            "--center-db",
            str(self.center),
            "--expect-usage",
            f"alpha:{ACCOUNT}",
            "--expect-quota",
            f"alpha:agy:{ACCOUNT}",
            "--max-age-seconds",
            "86400",
            "--format",
            "json",
        )
        self.assertEqual(
            healthy.returncode,
            0,
            healthy.stderr.decode() + healthy.stdout.decode(),
        )
        self.assertTrue(json.loads(healthy.stdout)["ok"])

        snapshots = self.tmp / "snapshots"
        backup = delivery_json(
            run_delivery_cli(
                "backup",
                "--center-db",
                str(self.center),
                "--dest",
                str(snapshots),
                "--format",
                "json",
            )
        )
        self.assertEqual(backup["integrity"], "ok")
        self.assertIn("events", backup["tables"])
        self.assertIn("quota_samples", backup["tables"])
        verified = delivery_json(
            run_delivery_cli(
                "verify-backup",
                "--backup",
                backup["path"],
                "--format",
                "json",
            )
        )
        self.assertTrue(verified["ok"])

        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.center) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        self.center.unlink()
        restored = delivery_json(
            run_delivery_cli(
                "restore",
                "--backup",
                backup["path"],
                "--center-db",
                str(self.center),
                "--format",
                "json",
            )
        )
        self.assertEqual(restored["integrity"], "ok")

        report = cli_json(
            run_cli(
                "report",
                "--center-db",
                str(self.center),
                "--month",
                "2026-11",
                "--format",
                "json",
            )
        )
        self.assertEqual(report["accounts"][ACCOUNT]["account_total"]["model_steps"], 1)
        quota_report = json.loads(
            run_quota_cli("report", "--db", str(self.center), "--json").stdout
        )
        account = [item for item in quota_report["accounts"] if item["provider"] == "agy"][0]
        self.assertEqual(len(account["windows"]), 1)
        self.assertEqual(account["windows"][0]["remaining"]["display_percent"], 50.0)


if __name__ == "__main__":
    unittest.main()
