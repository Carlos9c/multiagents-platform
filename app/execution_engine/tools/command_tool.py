from __future__ import annotations

import subprocess
from pathlib import Path

from app.execution_engine.contracts import CommandExecution


def run_command(
    *,
    command: str,
    cwd: str,
    timeout_seconds: int = 120,
) -> CommandExecution:
    working_dir = Path(cwd)
    if not working_dir.exists():
        raise FileNotFoundError(f"Working directory does not exist: {cwd}")

    completed = subprocess.run(
        command,
        cwd=str(working_dir),
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        encoding="utf-8",
        errors="replace",
    )

    return CommandExecution(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )