"""Constrained, non-interactive SSH transport for bounded delivery batches.

The client deliberately ignores the user's SSH configuration (``-F /dev/null``)
and pins the remote host key.  Forwarding, TTY allocation, agent use, password
authentication and local commands are disabled.  The remote command is a fixed
operator-supplied token sequence, never derived from a batch.

Only a validated JSON acknowledgement releases the client spool.  A timeout,
a non-zero exit status, malformed/oversized output or an acknowledgement for a
different binding counts as failure; the caller keeps the pending rows and
retries idempotently.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .delivery_bindings import Binding, KIND_AGY_USAGE, KIND_QUOTA_SAMPLE

DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_ACK_BYTES = 64 * 1024
DEFAULT_MAX_STDERR_BYTES = 16 * 1024
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9_.:@\[\]%+-]+$")
_SAFE_USER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_REMOTE_ARG_RE = re.compile(r"^[A-Za-z0-9_./:@=+-]+$")


class DeliveryTransportError(RuntimeError):
    """A bounded SSH delivery attempt did not produce a trustworthy ack."""


@dataclass(frozen=True)
class SSHConfig:
    host: str
    user: str
    identity_file: str
    known_hosts: str
    port: int = 22
    host_key_alias: str | None = None
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT
    timeout: float = DEFAULT_TIMEOUT
    max_batch_bytes: int = 256 * 1024
    max_ack_bytes: int = DEFAULT_MAX_ACK_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES
    ssh_binary: str = "ssh"
    remote_command: tuple[str, ...] = ("telemetre-receive",)

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not _SAFE_HOST_RE.match(self.host):
            raise DeliveryTransportError("ssh host contains unsafe characters")
        if self.host.startswith("-"):
            raise DeliveryTransportError("ssh host must not start with '-'")
        if not isinstance(self.user, str) or not _SAFE_USER_RE.match(self.user):
            raise DeliveryTransportError("ssh user contains unsafe characters")
        if isinstance(self.port, bool) or not 1 <= int(self.port) <= 65535:
            raise DeliveryTransportError("ssh port must be between 1 and 65535")
        if int(self.connect_timeout) < 1:
            raise DeliveryTransportError("connect_timeout must be positive")
        if float(self.timeout) <= 0:
            raise DeliveryTransportError("timeout must be positive")
        for name, value in (
            ("max_batch_bytes", self.max_batch_bytes),
            ("max_ack_bytes", self.max_ack_bytes),
            ("max_stderr_bytes", self.max_stderr_bytes),
        ):
            if int(value) < 1:
                raise DeliveryTransportError(f"{name} must be positive")
        if not self.remote_command:
            raise DeliveryTransportError("remote_command must not be empty")
        for item in self.remote_command:
            if not isinstance(item, str) or not _SAFE_REMOTE_ARG_RE.match(item):
                raise DeliveryTransportError(
                    "remote command arguments must be fixed safe tokens "
                    "(no spaces, quotes or shell metacharacters)"
                )


@dataclass(frozen=True)
class SSHTransport:
    """A ``Transport`` for :mod:`telemetre.replay` and quota replay."""

    binding: Binding
    config: SSHConfig

    def build_argv(self) -> list[str]:
        config = self.config
        argv = [
            config.ssh_binary,
            "-F",
            "/dev/null",
            "-T",
            "-p",
            str(config.port),
            "-i",
            str(Path(config.identity_file).expanduser()),
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "PubkeyAuthentication=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={Path(config.known_hosts).expanduser()}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            f"ConnectTimeout={int(config.connect_timeout)}",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "LogLevel=ERROR",
        ]
        if config.host_key_alias:
            if not _SAFE_HOST_RE.match(config.host_key_alias):
                raise DeliveryTransportError("host_key_alias contains unsafe characters")
            argv.extend(["-o", f"HostKeyAlias={config.host_key_alias}"])
        argv.extend([f"{config.user}@{config.host}", *config.remote_command])
        return argv

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["SSH_ASKPASS_REQUIRE"] = "never"
        env.pop("SSH_ASKPASS", None)
        env.pop("DISPLAY", None)
        return env

    def _check_local_files(self) -> None:
        identity = Path(self.config.identity_file).expanduser()
        known_hosts = Path(self.config.known_hosts).expanduser()
        if not identity.is_file():
            raise DeliveryTransportError(f"identity file does not exist: {identity}")
        try:
            mode = stat.S_IMODE(identity.stat().st_mode)
        except OSError as exc:
            raise DeliveryTransportError(f"cannot stat identity file: {exc}") from exc
        if mode & 0o077:
            raise DeliveryTransportError(
                "identity file must not be readable or writable by group/others"
            )
        if not known_hosts.is_file():
            raise DeliveryTransportError(f"known_hosts file does not exist: {known_hosts}")

    def send(self, batch: bytes) -> dict[str, Any]:
        if not isinstance(batch, (bytes, bytearray)):
            raise DeliveryTransportError("batch must be bytes")
        if len(batch) > self.config.max_batch_bytes:
            raise DeliveryTransportError(
                f"batch is {len(batch)} bytes, limit is {self.config.max_batch_bytes}"
            )
        self._check_local_files()
        try:
            argv = self.build_argv()
        except DeliveryTransportError:
            raise
        returncode, stdout, stderr, timed_out = _run_bounded(
            argv,
            bytes(batch),
            timeout=float(self.config.timeout),
            max_stdout=self.config.max_ack_bytes,
            max_stderr=self.config.max_stderr_bytes,
            env=self._environment(),
        )
        if timed_out:
            raise DeliveryTransportError(
                f"ssh delivery timed out after {self.config.timeout:g}s; pending rows retained"
            )
        if returncode != 0:
            detail = _clean_stderr(stderr)
            message = f"ssh delivery failed with exit {returncode}"
            if detail:
                message += f": {detail}"
            raise DeliveryTransportError(message)
        return parse_ack(stdout, self.binding, max_ack_bytes=self.config.max_ack_bytes)


def _clean_stderr(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return ""
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    return " | ".join(lines[:3])[:400]


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _run_bounded(
    argv: Sequence[str],
    payload: bytes,
    *,
    timeout: float,
    max_stdout: int,
    max_stderr: int,
    env: dict[str, str],
) -> tuple[int, bytes, bytes, bool]:
    """Run a child with a hard wall-clock limit and bounded output."""

    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
            close_fds=True,
        )
    except OSError as exc:
        raise DeliveryTransportError(f"cannot start ssh: {exc}") from exc

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdin_fd = process.stdin.fileno()
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    for descriptor in (stdin_fd, stdout_fd, stderr_fd):
        os.set_blocking(descriptor, False)

    selector = selectors.DefaultSelector()
    selector.register(stdin_fd, selectors.EVENT_WRITE, "stdin")
    selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
    selector.register(stderr_fd, selectors.EVENT_READ, "stderr")

    view = memoryview(payload)
    written = 0
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    overflow: str | None = None

    try:
        if not payload:
            selector.unregister(stdin_fd)
            try:
                process.stdin.close()
            except OSError:
                pass

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(remaining):
                stream = key.data
                if stream == "stdin":
                    try:
                        count = os.write(stdin_fd, view[written : written + 65536])
                        written += count
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError):
                        written = len(view)
                    if written >= len(view):
                        selector.unregister(stdin_fd)
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
                elif stream == "stdout":
                    chunk = os.read(stdout_fd, 65536)
                    if not chunk:
                        selector.unregister(stdout_fd)
                        continue
                    stdout.extend(chunk)
                    if len(stdout) > max_stdout:
                        overflow = f"ssh acknowledgement exceeds {max_stdout} bytes"
                        break
                else:
                    chunk = os.read(stderr_fd, 16384)
                    if not chunk:
                        selector.unregister(stderr_fd)
                        continue
                    stderr.extend(chunk)
                    if len(stderr) > max_stderr:
                        overflow = f"ssh diagnostics exceed {max_stderr} bytes"
                        break
                if overflow:
                    break
            if overflow:
                break
    finally:
        selector.close()
        for handle in (process.stdin, process.stdout, process.stderr):
            try:
                handle.close()
            except OSError:
                pass

    if overflow:
        _kill_group(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise DeliveryTransportError(overflow)

    remaining = max(0.0, deadline - time.monotonic())
    if not timed_out:
        try:
            process.wait(timeout=remaining or 0.01)
        except subprocess.TimeoutExpired:
            timed_out = True
    if timed_out:
        _kill_group(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return -1, bytes(stdout), bytes(stderr), True

    return int(process.returncode or 0), bytes(stdout), bytes(stderr), False


def parse_ack(stdout: bytes, binding: Binding, *, max_ack_bytes: int) -> dict[str, Any]:
    if len(stdout) > max_ack_bytes:
        raise DeliveryTransportError(f"ssh acknowledgement exceeds {max_ack_bytes} bytes")
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeliveryTransportError("ssh acknowledgement is not UTF-8") from exc
    if not text.strip():
        raise DeliveryTransportError("ssh acknowledgement is empty")
    try:
        ack = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeliveryTransportError("ssh acknowledgement is not valid JSON") from exc
    if not isinstance(ack, dict):
        raise DeliveryTransportError("ssh acknowledgement must be an object")
    if ack.get("ok") is not True:
        error = ack.get("error")
        detail = str(error)[:300] if error is not None else "receiver refused the batch"
        raise DeliveryTransportError(f"receiver refused the batch: {detail}")

    expected_kind = binding.kind
    if expected_kind not in (KIND_AGY_USAGE, KIND_QUOTA_SAMPLE):
        raise DeliveryTransportError("binding has an unsupported kind")
    for field in ("kind", "machine", "account"):
        if ack.get(field) != getattr(binding, field):
            raise DeliveryTransportError(
                f"acknowledgement {field} does not match the authenticated binding"
            )
    if binding.kind == KIND_QUOTA_SAMPLE:
        if ack.get("provider") != binding.provider:
            raise DeliveryTransportError(
                "acknowledgement provider does not match the authenticated binding"
            )
    binding_id = ack.get("binding_id")
    if binding_id is not None and binding_id != binding.binding_id:
        raise DeliveryTransportError(
            "acknowledgement binding_id does not match the authenticated binding"
        )
    for field in ("accepted", "stored", "duplicates"):
        value = ack.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DeliveryTransportError(f"acknowledgement {field} is not a non-negative integer")
    return ack


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_MAX_ACK_BYTES",
    "DEFAULT_TIMEOUT",
    "DeliveryTransportError",
    "SSHConfig",
    "SSHTransport",
    "parse_ack",
]
