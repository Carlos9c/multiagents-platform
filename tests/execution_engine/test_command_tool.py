from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.execution_engine.tools.command_tool import CommandToolError, run_command


def test_run_command_executes_simple_process(tmp_path: Path):
    result = run_command(
        command=f'{sys.executable} -c "print(123)"',
        cwd=str(tmp_path),
    )

    assert result.exit_code == 0
    assert "123" in result.stdout
    assert result.stderr == ""


def test_run_command_rejects_shell_executable(tmp_path: Path):
    with pytest.raises(CommandToolError, match="Shell executables are not allowed"):
        run_command(
            command='powershell -Command "Write-Output 123"',
            cwd=str(tmp_path),
        )


def test_run_command_rejects_empty_command(tmp_path: Path):
    with pytest.raises(CommandToolError, match="cannot be empty"):
        run_command(
            command="   ",
            cwd=str(tmp_path),
        )


def test_run_command_rejects_non_positive_timeout(tmp_path: Path):
    with pytest.raises(CommandToolError, match="greater than zero"):
        run_command(
            command=f'{sys.executable} -c "print(1)"',
            cwd=str(tmp_path),
            timeout_seconds=0,
        )


def test_run_command_rejects_timeout_above_limit(tmp_path: Path):
    with pytest.raises(CommandToolError, match="cannot exceed"):
        run_command(
            command=f'{sys.executable} -c "print(1)"',
            cwd=str(tmp_path),
            timeout_seconds=901,
        )


def test_run_command_rejects_unknown_executable(tmp_path: Path):
    with pytest.raises(CommandToolError, match="not available on PATH"):
        run_command(
            command="definitely_not_a_real_executable_12345",
            cwd=str(tmp_path),
        )


def test_run_command_rejects_path_argument_outside_workspace(tmp_path: Path):
    outside_file = tmp_path.parent / "outside.txt"
    outside_file.write_text("hello", encoding="utf-8")

    with pytest.raises(CommandToolError, match="outside the workspace"):
        run_command(
            command=f'"{sys.executable}" "{outside_file}"',
            cwd=str(tmp_path),
        )


def test_run_command_returns_timeout_result_instead_of_raising(tmp_path: Path):
    result = run_command(
        command=f'{sys.executable} -c "import time; time.sleep(2)"',
        cwd=str(tmp_path),
        timeout_seconds=1,
    )

    assert result.exit_code == 124
    assert "timed out" in result.stderr.lower()


def test_run_command_truncates_large_stdout(tmp_path: Path):
    result = run_command(
        command=f"{sys.executable} -c \"print('x' * 40000)\"",
        cwd=str(tmp_path),
    )

    assert result.exit_code == 0
    assert "truncated" in result.stdout
    assert len(result.stdout) <= 32000
