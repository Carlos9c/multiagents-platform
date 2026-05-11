from __future__ import annotations

import logging
from pathlib import Path

import docker
import docker.errors

from app.services.environment.base_driver import BaseEnvironmentDriver
from app.services.environment.contracts import (
    EnvironmentBootstrapError,
    EnvironmentCommandResult,
    EnvironmentSession,
    RuntimeSpec,
)

logger = logging.getLogger(__name__)

CONTAINER_MOUNT_POINT = "/project"
CONTAINER_NAME_PREFIX = "agente-env"

LOCK_FILE_NAMES: dict[str, str] = {
    "python_venv": "requirements.lock",
    "node_npm": "package.lock",
    "rust_cargo": "cargo.lock",
    "java_maven": "maven.lock",
}


def _build_install_command(spec: RuntimeSpec) -> str:
    if not spec.dependencies:
        return ""

    if spec.runtime_type == "python_venv":
        pkgs = []
        for dep in spec.dependencies:
            if dep.extras:
                extras_str = ",".join(dep.extras)
                pkgs.append(f"{dep.name}[{extras_str}]=={dep.version}")
            else:
                pkgs.append(f"{dep.name}=={dep.version}")
        return f"pip install --no-cache-dir {' '.join(pkgs)}"

    if spec.runtime_type == "node_npm":
        pkgs = [f"{dep.name}@{dep.version}" for dep in spec.dependencies]
        return f"npm install --save-exact {' '.join(pkgs)}"

    if spec.runtime_type == "java_maven":
        # Maven dependencies are declared in pom.xml by the code agents; no bootstrap install needed.
        return ""

    return ""


def _build_lock_command(spec: RuntimeSpec) -> str:
    if spec.runtime_type == "python_venv":
        return "pip freeze"
    if spec.runtime_type == "node_npm":
        return "npm list --json --depth=0"
    if spec.runtime_type == "java_maven":
        return "mvn --version"
    return "echo '{}'"


def _build_smoke_test_command(spec: RuntimeSpec) -> str:
    if spec.runtime_type == "python_venv":
        if not spec.dependencies:
            return "python --version"
        names = [dep.name for dep in spec.dependencies]
        checks = ", ".join(f"version({n!r})" for n in names)
        return f"python -c \"from importlib.metadata import version; [{checks}]; print('ok')\""

    if spec.runtime_type == "node_npm":
        if not spec.dependencies:
            return "node --version"
        checks = "; ".join(f"require('{dep.name}')" for dep in spec.dependencies)
        return f"node -e \"{checks}; console.log('ok')\""

    if spec.runtime_type == "rust_cargo":
        return "cargo --version"

    if spec.runtime_type == "java_maven":
        return "mvn --version && java --version"

    return "echo ok"


_PROXY_ERROR_HINT = (
    "This looks like a proxy/firewall issue: Docker Desktop's containerd snapshotter "
    "is trying to download image blobs via a direct HTTPS connection that is blocked.\n"
    "Fix: open Docker Desktop → Settings → Resources → Proxies, enable 'Manual proxy "
    "configuration', and enter your corporate HTTPS proxy (e.g. http://proxy.corp:8080).\n"
    "Then restart Docker Desktop and retry."
)


def _raise_pull_error(image: str, error_text: str) -> None:
    if "no HTTPS proxy" in error_text or "direct connection" in error_text or "dial tcp" in error_text:
        detail = f"{error_text}\n\n{_PROXY_ERROR_HINT}"
    else:
        detail = f"{error_text}\nTry running manually: docker pull {image}"
    raise EnvironmentBootstrapError(
        f"Failed to pull Docker image {image!r}: {detail}"
    )


class DockerDriver(BaseEnvironmentDriver):
    """Environment driver that manages a persistent Docker container per project session.

    The container is started once and kept alive. All commands are run via
    docker exec, avoiding per-command startup overhead. The project root on the
    host is mounted at CONTAINER_MOUNT_POINT inside the container.
    """

    def __init__(self) -> None:
        self._client: docker.DockerClient | None = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _container_name(self, project_id: int) -> str:
        return f"{CONTAINER_NAME_PREFIX}-{project_id}"

    def _remove_existing_container(self, client: docker.DockerClient, name: str) -> None:
        try:
            existing = client.containers.get(name)
            logger.info("docker_driver_removing_existing_container name=%s", name)
            existing.stop(timeout=5)
            existing.remove()
        except docker.errors.NotFound:
            pass
        except Exception:
            logger.warning(
                "docker_driver_failed_to_remove_existing_container name=%s",
                name,
                exc_info=True,
            )

    def _ensure_image(self, client: docker.DockerClient, image: str) -> None:
        try:
            client.images.get(image)
            return
        except docker.errors.ImageNotFound:
            pass

        logger.info("docker_driver_pulling_image image=%s", image)
        # Use low-level streaming pull: the high-level images.pull() has a known issue on
        # Windows Docker named pipes where it silently ignores pull errors and then raises
        # ImageNotFound from self.get() at the end, giving a misleading trace.
        for event in client.api.pull(image, stream=True, decode=True):
            if "error" in event:
                error_text = event["error"].strip()
                _raise_pull_error(image, error_text)

        try:
            client.images.get(image)
        except docker.errors.ImageNotFound as exc:
            raise EnvironmentBootstrapError(
                f"Docker image {image!r} is not available after pull attempt.\n"
                f"Ensure Docker Desktop has internet access and try:\n"
                f"  docker pull {image}"
            ) from exc

    def start_session(
        self,
        project_id: int,
        project_root: Path,
        spec: RuntimeSpec,
    ) -> EnvironmentSession:
        client = self._get_client()
        name = self._container_name(project_id)

        self._remove_existing_container(client, name)
        self._ensure_image(client, spec.image)

        container = client.containers.run(
            image=spec.image,
            command="tail -f /dev/null",
            detach=True,
            remove=True,
            name=name,
            volumes={
                str(project_root.resolve()): {
                    "bind": CONTAINER_MOUNT_POINT,
                    "mode": "rw",
                }
            },
            environment=spec.environment_variables or {},
        )

        logger.info(
            "docker_driver_session_started project_id=%s container_id=%s image=%s",
            project_id,
            container.id,
            spec.image,
        )

        return EnvironmentSession(
            project_id=project_id,
            container_id=container.id,
            project_root=project_root.resolve(),
            runtime_type=spec.runtime_type,
            container_mount_point=CONTAINER_MOUNT_POINT,
        )

    def run_command(
        self,
        session: EnvironmentSession,
        command: str,
        cwd: Path,
        timeout_seconds: int = 120,
    ) -> EnvironmentCommandResult:
        client = self._get_client()
        container = client.containers.get(session.container_id)
        container_cwd = session.host_to_container_path(cwd.resolve())
        full_command = ["sh", "-c", f"cd {container_cwd} && {command}"]

        logger.debug(
            "docker_driver_exec project_id=%s command=%s cwd=%s",
            session.project_id,
            command,
            container_cwd,
        )

        exit_code, (stdout_bytes, stderr_bytes) = container.exec_run(
            cmd=full_command,
            demux=True,
        )

        return EnvironmentCommandResult(
            exit_code=exit_code if exit_code is not None else 1,
            stdout=(stdout_bytes or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_bytes or b"").decode("utf-8", errors="replace"),
        )

    def install_packages(
        self,
        session: EnvironmentSession,
        spec: RuntimeSpec,
    ) -> EnvironmentCommandResult:
        install_cmd = _build_install_command(spec)
        if not install_cmd:
            logger.info(
                "docker_driver_no_packages_to_install project_id=%s",
                session.project_id,
            )
            return EnvironmentCommandResult(
                exit_code=0,
                stdout="No packages to install.",
                stderr="",
            )

        logger.info(
            "docker_driver_installing_packages project_id=%s command=%s",
            session.project_id,
            install_cmd,
        )
        return self.run_command(session, install_cmd, session.project_root)

    def generate_lock_file(
        self,
        session: EnvironmentSession,
        spec: RuntimeSpec,
    ) -> str:
        lock_cmd = _build_lock_command(spec)
        result = self.run_command(session, lock_cmd, session.project_root)
        if not result.succeeded:
            logger.warning(
                "docker_driver_lock_file_generation_failed project_id=%s stderr=%s",
                session.project_id,
                result.stderr,
            )
            return result.stderr or "Lock file generation failed."
        return result.stdout

    def smoke_test_command(self, spec: RuntimeSpec) -> str:
        return _build_smoke_test_command(spec)

    def stop_session(self, session: EnvironmentSession) -> None:
        client = self._get_client()
        try:
            container = client.containers.get(session.container_id)
            container.stop(timeout=10)
            try:
                # force=True handles the case where stop() timed out and the
                # container is still technically running.
                container.remove(force=True)
            except docker.errors.APIError as remove_exc:
                # 409 with "already in progress" means Docker's own --rm cleanup
                # is racing with us — the container will be gone momentarily.
                if remove_exc.status_code == 409 and "already in progress" in str(remove_exc):
                    logger.info(
                        "docker_driver_container_removal_in_progress project_id=%s container_id=%s",
                        session.project_id,
                        session.container_id,
                    )
                else:
                    raise
            logger.info(
                "docker_driver_session_stopped project_id=%s container_id=%s",
                session.project_id,
                session.container_id,
            )
        except docker.errors.NotFound:
            logger.info(
                "docker_driver_container_already_removed project_id=%s",
                session.project_id,
            )
        except Exception:
            logger.warning(
                "docker_driver_stop_session_failed project_id=%s",
                session.project_id,
                exc_info=True,
            )
