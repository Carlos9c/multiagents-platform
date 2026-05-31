"""Shared Docker command runner for QA agents.

Attempts to reuse the project's active environment session. If no session
is available, returns a result indicating Docker is unavailable so agents
can gracefully fall back to source-only analysis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120


@dataclass
class DockerRunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    no_session: bool = False

    @property
    def succeeded(self) -> bool:
        return not self.no_session and not self.timed_out and self.exit_code == 0

    @property
    def available(self) -> bool:
        return not self.no_session


_NO_SESSION = DockerRunResult(
    exit_code=1,
    stdout="",
    stderr="No active Docker session for this project",
    no_session=True,
)


def run_in_project_container(
    *,
    project_id: int,
    command: str,
    cwd: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
) -> DockerRunResult:
    """Run a command in the project's active Docker container.

    Returns a DockerRunResult. If no session is active, returns
    a result with no_session=True so callers can degrade gracefully.
    """
    try:
        from app.services.environment.docker_driver import DockerDriver
        from app.services.environment.session_store import get_session_store

        store = get_session_store()
        session = store.get_session(project_id)
        if session is None:
            logger.debug("qa_docker_runner_no_session project_id=%s", project_id)
            return _NO_SESSION

        driver = DockerDriver()
        result = driver.run_command(
            session=session,
            command=command,
            cwd=Path(cwd),
            timeout_seconds=timeout_seconds,
        )
        return DockerRunResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    except Exception as exc:
        logger.warning(
            "qa_docker_runner_error project_id=%s command=%s exc=%s",
            project_id,
            command,
            exc,
        )
        return DockerRunResult(
            exit_code=1,
            stdout="",
            stderr=str(exc),
            no_session=True,
        )
