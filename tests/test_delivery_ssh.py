"""Constrained SSH client checks: pinning, bounds, timeout and ack validation."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from telemetre.delivery_bindings import Binding
from telemetre.delivery_ssh import (
    DeliveryTransportError,
    SSHConfig,
    SSHTransport,
    parse_ack,
)
from tests.test_delivery_support import (
    argv_log_for,
    make_ssh_files,
    set_fake_mode,
    write_bindings,
    write_fake_ssh,
)

BINDING = Binding(
    binding_id="alpha-usage",
    machine="alpha",
    account="acct@example.test",
    kind="agy_usage",
)


def _config(directory: Path, *, ssh_binary: str = "ssh", timeout: float = 5.0) -> SSHConfig:
    identity, known_hosts = make_ssh_files(directory)
    return SSHConfig(
        host="center.example",
        user="telemetre",
        identity_file=str(identity),
        known_hosts=str(known_hosts),
        ssh_binary=ssh_binary,
        timeout=timeout,
        max_batch_bytes=1024,
    )


class SSHArgumentTest(unittest.TestCase):
    def test_argv_pins_the_host_key_and_disables_interactive_and_forwarding_features(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            argv = SSHTransport(BINDING, config).build_argv()
            text = " ".join(argv)
            self.assertIn("-F /dev/null", text)
            self.assertIn("-T", argv)
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("PasswordAuthentication=no", argv)
            self.assertIn("KbdInteractiveAuthentication=no", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn(f"UserKnownHostsFile={config.known_hosts}", argv)
            self.assertIn("GlobalKnownHostsFile=/dev/null", argv)
            self.assertIn("UpdateHostKeys=no", argv)
            self.assertIn("ClearAllForwardings=yes", argv)
            self.assertIn("ForwardAgent=no", argv)
            self.assertIn("ForwardX11=no", argv)
            self.assertEqual(argv[-2:], ["telemetre@center.example", "telemetre-receive"])

    def test_oversized_batch_is_rejected_before_ssh_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp), ssh_binary="/nonexistent/ssh")
            transport = SSHTransport(BINDING, config)
            with self.assertRaises(DeliveryTransportError) as raised:
                transport.send(b"x" * (config.max_batch_bytes + 1))
            self.assertIn("limit", str(raised.exception))
            self.assertNotIn("cannot start", str(raised.exception))

    def test_identity_file_must_not_be_group_or_world_accessible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            Path(config.identity_file).chmod(0o644)
            with self.assertRaises(DeliveryTransportError) as raised:
                SSHTransport(BINDING, config).send(b"{}")
            self.assertIn("group/others", str(raised.exception))


class SSHExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.bindings = write_bindings(
            self.tmp / "bindings.json",
            {
                "alpha-usage": {
                    "machine": "alpha",
                    "account": "acct@example.test",
                    "kind": "agy_usage",
                }
            },
        )
        self.center = self.tmp / "center.sqlite3"

    def _transport(self, mode: str, *, timeout: float = 5.0) -> tuple[SSHTransport, Path]:
        script = write_fake_ssh(
            self.tmp,
            name=f"fake-ssh-{mode}",
            binding_id="alpha-usage",
            bindings_path=self.bindings,
            center_db=self.center,
            mode=mode,
        )
        return SSHTransport(BINDING, _config(self.tmp, ssh_binary=str(script), timeout=timeout)), script

    def test_hard_timeout_kills_ssh_and_reports_a_retryable_failure(self) -> None:
        transport, script = self._transport("timeout", timeout=0.4)
        started = time.monotonic()
        with self.assertRaises(DeliveryTransportError) as raised:
            transport.send(b"{}")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertIn("timed out", str(raised.exception))
        self.assertTrue(argv_log_for(script))

    def test_host_key_failure_and_malformed_ack_are_rejected(self) -> None:
        transport, _ = self._transport("hostkey")
        with self.assertRaises(DeliveryTransportError) as raised:
            transport.send(b"{}")
        self.assertIn("exit 255", str(raised.exception))
        self.assertIn("Host key verification failed", str(raised.exception))

        set_fake_mode(Path(str(transport.config.ssh_binary)), "garbage")
        # The mode file belongs to this script; switching it keeps the same
        # transport and proves the client never deletes pending rows on a bad ack.
        script = Path(transport.config.ssh_binary)
        set_fake_mode(script, "garbage")
        with self.assertRaises(DeliveryTransportError) as raised:
            transport.send(b"{}")
        self.assertIn("not valid JSON", str(raised.exception))

    def test_oversized_ack_is_bounded(self) -> None:
        transport, _ = self._transport("oversize-ack")
        with self.assertRaises(DeliveryTransportError) as raised:
            transport.send(b"{}")
        self.assertIn("exceeds", str(raised.exception))


class AckValidationTest(unittest.TestCase):
    def _ack(self, **overrides) -> dict:
        ack = {
            "ok": True,
            "kind": "agy_usage",
            "machine": "alpha",
            "account": "acct@example.test",
            "binding_id": "alpha-usage",
            "accepted": 1,
            "stored": 1,
            "duplicates": 0,
        }
        ack.update(overrides)
        return ack

    def test_ack_for_another_identity_is_rejected(self) -> None:
        for overrides in (
            {"machine": "beta"},
            {"account": "other@example.test"},
            {"kind": "quota_sample"},
            {"binding_id": "beta-usage"},
            {"duplicates": -1},
            {"stored": True},
            {"ok": False, "error": "no"},
        ):
            with self.subTest(overrides=overrides):
                import json

                with self.assertRaises(DeliveryTransportError):
                    parse_ack(
                        json.dumps(self._ack(**overrides)).encode(),
                        BINDING,
                        max_ack_bytes=64 * 1024,
                    )

    def test_ack_for_the_bound_identity_is_accepted(self) -> None:
        import json

        ack = parse_ack(
            json.dumps(self._ack()).encode(),
            BINDING,
            max_ack_bytes=64 * 1024,
        )
        self.assertEqual(ack["machine"], "alpha")


if __name__ == "__main__":
    unittest.main()
