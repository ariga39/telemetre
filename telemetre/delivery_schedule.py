"""Single-instance scheduled jobs with a hard wall-clock timeout.

The lock is an advisory ``flock`` on a file; the kernel releases it even if a
job is killed.  A second invocation reports ``already running`` and does no
work.  A timed-out job is terminated together with its process group so a
provider child cannot keep running after the timer stopped.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


class ScheduleError(RuntimeError):
    """A scheduled job could not acquire its lock or start safely."""


def _kill_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def run_locked_job(
    command: Sequence[str],
    *,
    lock_path: str | Path,
    timeout: float,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("command must be a non-empty list of strings")
    if float(timeout) <= 0:
        raise ValueError("timeout must be positive")
    path = Path(lock_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        lock_handle = path.open("a+")
    except OSError as exc:
        raise ScheduleError(f"cannot open lock file: {exc}") from exc
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "ran": False,
                "reason": "already running",
                "lock_path": str(path),
                "timed_out": False,
                "exit_code": None,
            }

        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(
            f"pid={os.getpid()}\n"
            f"started_at={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        )
        lock_handle.flush()
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                list(command),
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return {
                "ran": True,
                "reason": "job started",
                "lock_path": str(path),
                "exit_code": 127,
                "timed_out": False,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": "",
                "stderr": "",
                "error": f"cannot start job: {exc}",
            }

        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=float(timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process)
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        duration = round(time.monotonic() - started, 3)

        result: dict[str, Any] = {
            "ran": True,
            "reason": "timed out" if timed_out else "job finished",
            "lock_path": str(path),
            "exit_code": None if timed_out else int(process.returncode or 0),
            "timed_out": timed_out,
            "duration_seconds": duration,
            "stdout": stdout,
            "stderr": stderr,
        }
        if timed_out:
            result["error"] = f"job exceeded {float(timeout):g}s and was terminated"
            result["exit_code"] = 124
        return result
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()


__all__ = ["ScheduleError", "run_locked_job"]
