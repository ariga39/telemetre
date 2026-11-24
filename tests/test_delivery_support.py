"""Shared fixtures for delivery behavior tests.

Everything is synthetic and local: fake SSH binaries, temporary SQLite files
and generated transcripts.  No test reads a real credential or contacts a real
host.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_delivery_cli(*args: str, stdin: bytes | None = None, cwd: Path | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "telemetre.delivery", *args],
        input=stdin,
        cwd=str(cwd or REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run_quota_cli(*args: str, cwd: Path | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "telemetre.quota", *args],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def delivery_json(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, (
        f"command failed ({result.returncode})\n"
        f"stdout:\n{result.stdout.decode(errors='replace')}\n"
        f"stderr:\n{result.stderr.decode(errors='replace')}"
    )
    return json.loads(result.stdout.decode())


def ssh_cli_args(
    directory: Path,
    script: Path,
    *,
    timeout: float = 5.0,
    max_bytes: int = 64 * 1024,
) -> list[str]:
    identity, known_hosts = make_ssh_files(directory)
    return [
        "--ssh-host",
        "center.example",
        "--ssh-user",
        "telemetre",
        "--identity",
        str(identity),
        "--known-hosts",
        str(known_hosts),
        "--ssh-binary",
        str(script),
        "--timeout",
        str(timeout),
        "--max-bytes",
        str(max_bytes),
    ]


def write_bindings(path: Path, bindings: dict[str, dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"bindings": bindings}, indent=2), encoding="utf-8")
    return path


def make_ssh_files(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    identity = directory / "id_test"
    identity.write_text("synthetic-key\n", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts = directory / "known_hosts"
    known_hosts.write_text("center.example ssh-ed25519 AAAA\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    return identity, known_hosts


def write_fake_ssh(
    directory: Path,
    *,
    name: str,
    binding_id: str,
    bindings_path: Path,
    center_db: Path,
    mode: str = "ok",
    receiver_max_bytes: int | None = None,
) -> Path:
    """Write an `ssh` stand-in that runs the real forced-command receiver.

    The mode file next to the script can switch between ok, drop-ack, hostkey,
    timeout, garbage and oversize-ack without changing the transport.
    """

    directory.mkdir(parents=True, exist_ok=True)
    mode_file = directory / f"{name}.mode"
    mode_file.write_text(mode, encoding="utf-8")
    argv_log = directory / f"{name}.argv.json"
    receiver_args = [
        sys.executable,
        "-m",
        "telemetre.delivery",
        "receive",
        "--bindings",
        str(bindings_path),
        "--binding-id",
        binding_id,
        "--center-db",
        str(center_db),
    ]
    if receiver_max_bytes is not None:
        receiver_args.extend(["--max-bytes", str(receiver_max_bytes)])
    script = directory / name
    script.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time

MODE_FILE = {str(mode_file)!r}
ARGV_LOG = {str(argv_log)!r}
RECEIVER = {receiver_args!r}
with open(ARGV_LOG, "w", encoding="utf-8") as handle:
    json.dump(sys.argv[1:], handle, indent=2)

try:
    mode = open(MODE_FILE, encoding="utf-8").read().strip() or "ok"
except OSError:
    mode = "ok"

if mode == "hostkey":
    sys.stderr.write("Host key verification failed.\\n")
    sys.exit(255)
if mode == "timeout":
    sys.stdin.buffer.read()
    time.sleep(60)
    sys.exit(0)

data = sys.stdin.buffer.read()
env = os.environ.copy()
env["PYTHONPATH"] = {str(REPO_ROOT)!r} + os.pathsep + env.get("PYTHONPATH", "")
result = subprocess.run(RECEIVER, input=data, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, env=env, check=False)
with open(ARGV_LOG + ".receiver-stdout", "wb") as handle:
    handle.write(result.stdout)
with open(ARGV_LOG + ".receiver-stderr", "wb") as handle:
    handle.write(result.stderr)
if mode == "drop-ack":
    sys.stderr.write(result.stderr.decode("utf-8", "replace"))
    sys.exit(1)
if mode == "garbage":
    sys.stdout.write("not-json\\n")
    sys.exit(0)
if mode == "oversize-ack":
    sys.stdout.write("{{" + "x" * 200000 + "}}\\n")
    sys.exit(0)
sys.stdout.buffer.write(result.stdout)
if result.returncode != 0:
    sys.stderr.write("receiver stdout: " + result.stdout.decode("utf-8", "replace") + "\\n")
sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def argv_log_for(script: Path) -> dict:
    return json.loads(script.with_suffix(".argv.json").read_text(encoding="utf-8"))


def set_fake_mode(script: Path, mode: str) -> None:
    script.with_suffix(".mode").write_text(mode, encoding="utf-8")


def pid_is_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc_stat = Path(f"/proc/{pid}/stat")
        if not proc_stat.exists():
            return True
        try:
            fields = proc_stat.read_text(encoding="utf-8").split()
        except OSError:
            return True
        if len(fields) > 2 and fields[2] == "Z":
            return True
        time.sleep(0.05)
    return False
