from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from app.execution_engine.contracts import CommandExecution

MAX_COMMAND_OUTPUT_CHARS = 32_000
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 900

FORBIDDEN_EXECUTABLES = {
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "bash",
    "zsh",
    "fish",
}


class CommandToolError(ValueError):
    """Raised when a command is invalid or unsafe for the execution engine."""


def _truncate_output(value: str | None, *, label: str) -> str:
    text = value or ""
    if len(text) <= MAX_COMMAND_OUTPUT_CHARS:
        return text

    truncated_count = len(text) - MAX_COMMAND_OUTPUT_CHARS
    suffix = (
        f"\n[truncated {truncated_count} characters from {label} "
        f"to keep command output bounded]"
    )
    allowed = max(0, MAX_COMMAND_OUTPUT_CHARS - len(suffix))
    return text[:allowed] + suffix


def _validate_timeout(timeout_seconds: int) -> int:
    if not isinstance(timeout_seconds, int):
        raise CommandToolError("timeout_seconds must be an integer.")
    if timeout_seconds <= 0:
        raise CommandToolError("timeout_seconds must be greater than zero.")
    if timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise CommandToolError(f"timeout_seconds cannot exceed {MAX_TIMEOUT_SECONDS} seconds.")
    return timeout_seconds


def _validate_working_directory(cwd: str) -> Path:
    working_dir = Path(cwd).expanduser()
    if not working_dir.exists():
        raise FileNotFoundError(f"Working directory does not exist: {cwd}")
    if not working_dir.is_dir():
        raise NotADirectoryError(f"Working directory is not a directory: {cwd}")
    return working_dir.resolve()


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _validate_and_parse_command(command: str) -> list[str]:
    normalized = (command or "").strip()
    if not normalized:
        raise CommandToolError("Command cannot be empty.")

    if "\x00" in normalized:
        raise CommandToolError("Command cannot contain NUL bytes.")

    try:
        argv = shlex.split(normalized, posix=(os.name != "nt"))
    except ValueError as exc:
        raise CommandToolError(f"Command could not be parsed safely: {str(exc)}") from exc

    if not argv:
        raise CommandToolError("Command cannot be empty after parsing.")

    return [_strip_wrapping_quotes(arg) for arg in argv]


def _validate_executable(argv: list[str]) -> None:
    executable = (argv[0] or "").strip()
    if not executable:
        raise CommandToolError("Command executable cannot be empty.")

    executable_name = Path(executable).name.lower()
    if executable_name in FORBIDDEN_EXECUTABLES:
        raise CommandToolError(f"Shell executables are not allowed: {executable_name}")

    executable_path = Path(executable)

    if executable_path.is_absolute():
        if not executable_path.exists():
            raise CommandToolError(f"Command executable does not exist: {executable}")
        return

    if shutil.which(executable) is None:
        raise CommandToolError(f"Command executable is not available on PATH: {executable}")


def _looks_like_path_argument(argument: str) -> bool:
    if not argument or argument.startswith("-"):
        return False

    if argument.startswith(("./", "../", ".\\", "..\\")):
        return True

    if "/" in argument or "\\" in argument:
        return True

    if Path(argument).is_absolute():
        return True

    return False


def _validate_path_arguments_within_execution_tree(argv: list[str], working_dir: Path) -> None:
    for argument in argv[1:]:
        if not _looks_like_path_argument(argument):
            continue

        candidate = Path(argument).expanduser()

        if candidate.is_absolute():
            resolved_candidate = candidate.resolve()
        else:
            resolved_candidate = (working_dir / candidate).resolve()

        try:
            resolved_candidate.relative_to(working_dir)
        except ValueError as exc:
            raise CommandToolError(
                "Command contains a path argument outside the execution tree: " f"{argument}"
            ) from exc


def run_command(
    *,
    command: str,
    cwd: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> CommandExecution:
    validated_timeout = _validate_timeout(timeout_seconds)
    working_dir = _validate_working_directory(cwd)
    argv = _validate_and_parse_command(command)

    _validate_executable(argv)
    _validate_path_arguments_within_execution_tree(argv, working_dir)

    try:
        completed = subprocess.run(
            argv,
            cwd=str(working_dir),
            shell=False,
            capture_output=True,
            text=True,
            timeout=validated_timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""

        timeout_message = f"Command timed out after {validated_timeout} seconds."
        combined_stderr = stderr.strip()
        if combined_stderr:
            combined_stderr = f"{combined_stderr}\n{timeout_message}"
        else:
            combined_stderr = timeout_message

        return CommandExecution(
            command=command,
            exit_code=124,
            stdout=_truncate_output(stdout, label="stdout"),
            stderr=_truncate_output(combined_stderr, label="stderr"),
        )
    except FileNotFoundError as exc:
        raise CommandToolError(f"Command executable could not be started: {str(exc)}") from exc

    return CommandExecution(
        command=command,
        exit_code=completed.returncode,
        stdout=_truncate_output(completed.stdout, label="stdout"),
        stderr=_truncate_output(completed.stderr, label="stderr"),
    )
