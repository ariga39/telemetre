"""Shared helpers for CLI-boundary behavior tests.

All fixtures are synthetic.  Never point these helpers at a real agy data root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_transcript(root: Path, conversation_id: str, rows: list[dict]) -> Path:
    """Create brain/<id>/.system_generated/logs/transcript.jsonl under root."""
    path = (
        root
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    path.write_text(body, encoding="utf-8")
    return path


def append_raw(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def run_cli(*args: str, stdin: bytes | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "telemetre", *args],
        input=stdin,
        cwd=str(cwd or REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def cli_json(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, (
        f"command failed ({result.returncode})\n"
        f"stdout:\n{result.stdout.decode()}\n"
        f"stderr:\n{result.stderr.decode()}"
    )
    return json.loads(result.stdout.decode())


def model_step(
    step_index: int,
    created_at: str,
    model: str = "gemini-3-pro",
    *,
    input_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    output_tokens: int | None = None,
    include: tuple[str, ...] = ("input_tokens", "cache_read_tokens", "output_tokens"),
) -> dict:
    row: dict = {
        "step_index": step_index,
        "created_at": created_at,
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "model": model,
    }
    values = {
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read_tokens,
        "output_tokens": output_tokens,
    }
    for field in include:
        row[field] = values[field]
    return row
