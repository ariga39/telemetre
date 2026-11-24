"""Shared fixtures for the quota vertical-slice tests.

All provider processes used by these tests are synthetic local scripts.  No
test reads a real provider log, credential, or network service.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, env: dict | None = None, timeout: float = 20.0):
    command = [sys.executable, "-m", "telemetre.quota", *map(str, args)]
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + merged_env["PYTHONPATH"] if merged_env.get("PYTHONPATH") else ""
    )
    if env:
        merged_env.update(env)
    return subprocess.run(
        command,
        cwd=ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def json_output(process: subprocess.CompletedProcess):
    if not process.stdout.strip():
        raise AssertionError(
            f"no JSON on stdout (exit {process.returncode})\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    return json.loads(process.stdout)


def write_executable(directory: Path, name: str, source: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)
    return path


def write_fake_agy(
    directory: Path,
    *,
    payload=None,
    raw_stdout: str | None = None,
    exit_code: int = 0,
    stderr: str = "",
    sleep_seconds: float = 0.0,
    spawn_child_pidfile: Path | None = None,
) -> Path:
    payload_text = (
        raw_stdout
        if raw_stdout is not None
        else json.dumps(payload if payload is not None else {})
    )
    child_block = ""
    if spawn_child_pidfile is not None:
        child_block = f"""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(spawn_child_pidfile)!r}, "w", encoding="utf-8").write(str(child.pid))
        """
    return write_executable(
        directory,
        "fake-agy",
        f"""
        #!/usr/bin/env python3
        import json
        import subprocess
        import sys
        import time

        {child_block}
        if {sleep_seconds!r}:
            time.sleep({sleep_seconds!r})
        sys.stderr.write({stderr!r})
        sys.stdout.write({payload_text!r})
        sys.exit({exit_code!r})
        """,
    )


def write_fake_codex(
    directory: Path,
    *,
    result,
    init_result=None,
    read_error=None,
    init_error=None,
    init_delay_seconds: float = 0.0,
    hang_after_response: bool = False,
    spawn_child_pidfile: Path | None = None,
) -> Path:
    if init_result is None:
        init_result = {"userAgent": "synthetic-codex-test"}
    child_block = ""
    if spawn_child_pidfile is not None:
        child_block = f"""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(spawn_child_pidfile)!r}, "w", encoding="utf-8").write(str(child.pid))
        """
    return write_executable(
        directory,
        "fake-codex",
        f"""
        #!/usr/bin/env python3
        import json
        import subprocess
        import sys
        import time

        RESULT = json.loads({json.dumps(result)!r})
        INIT_RESULT = json.loads({json.dumps(init_result)!r})
        INIT_ERROR = json.loads({json.dumps(init_error)!r})
        READ_ERROR = json.loads({json.dumps(read_error)!r})

        {child_block}
        initialized = False

        def send(message):
            sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\\n")
            sys.stdout.flush()

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialize":
                if {init_delay_seconds!r}:
                    time.sleep({init_delay_seconds!r})
                if INIT_ERROR is not None:
                    send({{"id": request_id, "error": INIT_ERROR}})
                else:
                    send({{"id": request_id, "result": INIT_RESULT}})
            elif method == "initialized":
                initialized = True
            elif method == "account/rateLimits/read":
                if not initialized:
                    send({{"id": request_id, "error": {{"code": "not_initialized", "message": "initialize first"}}}})
                elif READ_ERROR is not None:
                    send({{"id": request_id, "error": READ_ERROR}})
                else:
                    send({{"id": request_id, "result": RESULT}})
                    if {hang_after_response!r}:
                        time.sleep(60)
            else:
                send({{"id": request_id, "error": {{"code": "unknown_method", "message": method}}}})
        """,
    )


def write_config(
    directory: Path,
    sources,
    *,
    machine: str = "test-machine",
    database: Path | None = None,
    timezone: str = "UTC",
) -> Path:
    config = {
        "machine": machine,
        "timezone": timezone,
        "providers": list(sources),
    }
    if database is not None:
        config["database"] = str(database)
    path = directory / "quota.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def source(provider: str, account: str, command: Path, **overrides):
    data = {
        "provider": provider,
        "account": account,
        "command": [sys.executable, str(command)],
        "interval_seconds": 900,
        "timeout_seconds": 5,
    }
    data.update(overrides)
    return data


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
