from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from app.execution_engine.contracts import CommandExecution


FORBIDDEN_SHELL_TOKENS = [
    "&&",
    "||",
    ";",
    "|",
    ">",
    ">>",
    "<",
    "2>",
    "&",
]


class CommandToolError(ValueError):
    """Raised when a command is invalid or unsafe for the execution engine."""


def _validate_and_parse_command(command: str) -> list[str]:
    normalized = (command or "").strip()
    if not normalized:
        raise CommandToolError("Command cannot be empty.")

    for token in FORBIDDEN_SHELL_TOKENS:
        if token in normalized:
            raise CommandToolError(
                f"Command contains forbidden shell operator '{token}'."
            )

    try:
        argv = shlex.split(normalized, posix=False)
    except ValueError as exc:
        raise CommandToolError(
            f"Command could not be parsed safely: {str(exc)}"
        ) from exc

    if not argv:
        raise CommandToolError("Command cannot be empty after parsing.")

    return argv


def run_command(
    *,
    command: str,
    cwd: str,
    timeout_seconds: int = 120,
) -> CommandExecution:
    working_dir = Path(cwd)
    if not working_dir.exists():
        raise FileNotFoundError(f"Working directory does not exist: {cwd}")

    argv = _validate_and_parse_command(command)

    completed = subprocess.run(
        argv,
        cwd=str(working_dir),
        shell=False,
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
