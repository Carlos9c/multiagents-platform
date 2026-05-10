from __future__ import annotations

import logging

from app.services.environment.contracts import (
    EnvironmentBootstrapError,
    EnvironmentSession,
    RuntimeSpec,
)
from app.services.environment.docker_driver import LOCK_FILE_NAMES
from app.services.environment.registry import DriverRegistry, get_driver_registry
from app.services.environment.session_store import EnvironmentSessionStore, get_session_store
from app.services.project_storage import ProjectStorageService

logger = logging.getLogger(__name__)


class EnvironmentBootstrapper:
    """Orchestrates environment startup: container launch, package install, lock file."""

    def __init__(
        self,
        registry: DriverRegistry | None = None,
        session_store: EnvironmentSessionStore | None = None,
        storage_service: ProjectStorageService | None = None,
    ) -> None:
        self.registry = registry or get_driver_registry()
        self.session_store = session_store or get_session_store()
        self.storage_service = storage_service or ProjectStorageService()

    def bootstrap(self, project_id: int, spec: RuntimeSpec) -> EnvironmentSession:
        """Start the container, install packages, and persist the lock file.

        Stores the session in the session store. Raises EnvironmentBootstrapError
        if installation fails. The caller must call teardown() when done.
        """
        driver = self.registry.get_driver(spec.runtime_type)
        paths = self.storage_service.ensure_project_storage(project_id)

        logger.info(
            "environment_bootstrapper_starting project_id=%s image=%s packages=%d",
            project_id,
            spec.image,
            len(spec.dependencies),
        )

        session = driver.start_session(
            project_id=project_id,
            project_root=paths.project_root,
            spec=spec,
        )
        self.session_store.set_session(project_id, session)

        install_result = driver.install_packages(session, spec)
        if not install_result.succeeded:
            driver.stop_session(session)
            self.session_store.remove_session(project_id)
            raise EnvironmentBootstrapError(
                f"Package installation failed for project {project_id}.\n"
                f"stdout: {install_result.stdout}\n"
                f"stderr: {install_result.stderr}"
            )

        lock_content = driver.generate_lock_file(session, spec)
        lock_filename = LOCK_FILE_NAMES.get(spec.runtime_type, "env.lock")
        lock_path = paths.env_dir / lock_filename
        lock_path.write_text(lock_content, encoding="utf-8")

        logger.info(
            "environment_bootstrapper_ready project_id=%s lock_file=%s",
            project_id,
            str(lock_path),
        )

        return session

    def teardown(self, project_id: int) -> None:
        """Stop the container and remove the session from the store."""
        session = self.session_store.get_session(project_id)
        if session is None:
            logger.debug("environment_bootstrapper_teardown_no_session project_id=%s", project_id)
            return

        driver = self.registry.get_driver(session.runtime_type)
        driver.stop_session(session)
        self.session_store.remove_session(project_id)

        logger.info("environment_bootstrapper_teardown_done project_id=%s", project_id)
