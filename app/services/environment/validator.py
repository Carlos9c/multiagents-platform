from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.services.environment.contracts import (
    EnvironmentValidationError,
    PinnedDependency,
    RuntimeSpec,
    SpecChange,
)
from app.services.environment.planner_client import call_environment_repair
from app.services.environment.registry import DriverRegistry, get_driver_registry
from app.services.environment.session_store import EnvironmentSessionStore, get_session_store

logger = logging.getLogger(__name__)


class EnvironmentValidator:
    """Runs smoke tests against the active environment session.

    On failure, attempts one LLM-guided repair: corrects the spec, reinstalls
    packages, and re-runs the smoke test. If the second attempt also fails,
    raises EnvironmentValidationError with full context for manual review.
    """

    def __init__(
        self,
        registry: DriverRegistry | None = None,
        session_store: EnvironmentSessionStore | None = None,
    ) -> None:
        self.registry = registry or get_driver_registry()
        self.session_store = session_store or get_session_store()

    def validate(self, project_id: int, spec: RuntimeSpec) -> RuntimeSpec:
        """Run the smoke test. Returns the final spec (possibly repaired).

        Raises EnvironmentValidationError if validation cannot be recovered.
        """
        session = self.session_store.get_session(project_id)
        if session is None:
            raise EnvironmentValidationError(
                f"No active environment session for project {project_id}.",
                spec=spec,
                smoke_test_output="",
            )

        driver = self.registry.get_driver(spec.runtime_type)
        smoke_cmd = driver.smoke_test_command(spec)

        logger.info(
            "environment_validator_smoke_test project_id=%s command=%s",
            project_id,
            smoke_cmd,
        )

        result = driver.run_command(session, smoke_cmd, session.project_root)

        if result.succeeded:
            logger.info("environment_validator_passed project_id=%s", project_id)
            return spec

        first_error = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        logger.warning(
            "environment_validator_smoke_test_failed project_id=%s attempting_repair",
            project_id,
        )

        try:
            repair_output = call_environment_repair(
                spec_dict=spec.model_dump(),
                smoke_test_command=smoke_cmd,
                error_output=first_error,
                project_id=project_id,
            )
        except Exception as exc:
            raise EnvironmentValidationError(
                "Smoke test failed and LLM repair call errored.",
                spec=spec,
                smoke_test_output=first_error,
                repair_attempt_output=str(exc),
            ) from exc

        corrected_spec = RuntimeSpec(
            runtime_type=spec.runtime_type,
            image=repair_output.corrected_image,
            dependencies=[
                PinnedDependency(
                    name=dep.name,
                    version=dep.version,
                    extras=dep.extras,
                )
                for dep in repair_output.corrected_dependencies
            ],
            environment_variables=spec.environment_variables,
            pending_additions=spec.pending_additions,
            change_log=[
                *spec.change_log,
                SpecChange(
                    change_type="repair",
                    packages_affected=[d.name for d in repair_output.corrected_dependencies],
                    reason=repair_output.repair_rationale,
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                ),
            ],
        )

        install_result = driver.install_packages(session, corrected_spec)
        repair_install_output = (
            f"stdout:\n{install_result.stdout}\nstderr:\n{install_result.stderr}"
        )

        if not install_result.succeeded:
            raise EnvironmentValidationError(
                "Environment repair failed: package installation error after repair.",
                spec=corrected_spec,
                smoke_test_output=first_error,
                repair_attempt_output=repair_install_output,
            )

        smoke_cmd2 = driver.smoke_test_command(corrected_spec)
        result2 = driver.run_command(session, smoke_cmd2, session.project_root)

        if result2.succeeded:
            logger.info("environment_validator_repair_succeeded project_id=%s", project_id)
            return corrected_spec

        second_error = f"stdout:\n{result2.stdout}\nstderr:\n{result2.stderr}"
        raise EnvironmentValidationError(
            "Environment validation failed after repair attempt. Manual review required.",
            spec=corrected_spec,
            smoke_test_output=first_error,
            repair_attempt_output=second_error,
        )
