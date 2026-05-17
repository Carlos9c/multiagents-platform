from __future__ import annotations

import logging
from pathlib import Path

from app.core.config import settings
from app.services.environment.base_driver import BaseEnvironmentDriver
from app.services.environment.contracts import (
    EnvironmentBootstrapError,
    EnvironmentSession,
    RuntimeSpec,
)
from app.services.environment.docker_driver import LOCK_FILE_NAMES
from app.services.environment.registry import DriverRegistry, get_driver_registry
from app.services.environment.session_store import EnvironmentSessionStore, get_session_store
from app.services.project_storage import ProjectStorageService

_GRADLE_DISTRIBUTION_URL_TEMPLATE = (
    "https://services.gradle.org/distributions/gradle-{version}-bin.zip"
)

# Standard Gradle wrapper shell script (UN*X). Content is stable across Gradle 7.x–8.x.
# Written directly to source_dir on the host — no container or network access needed.
_GRADLEW_TEMPLATE = """\
#!/bin/sh
#
# Gradle startup script for UN*X
#
APP_HOME=$( cd "${0%"/${0##*/}"}/" && pwd -P )
APP_NAME="Gradle"
APP_BASE_NAME="${0##*/}"
DEFAULT_JVM_OPTS='"-Xmx64m" "-Xms64m"'
warn() { echo "$*" >&2; }
die() { echo; echo "$*" >&2; echo; exit 1; }
CLASSPATH="$APP_HOME/gradle/wrapper/gradle-wrapper.jar"
if [ -n "$JAVA_HOME" ] ; then
    if [ -x "$JAVA_HOME/jre/sh/java" ] ; then
        JAVACMD="$JAVA_HOME/jre/sh/java"
    else
        JAVACMD="$JAVA_HOME/bin/java"
    fi
    [ -x "$JAVACMD" ] || die "ERROR: JAVA_HOME is set to an invalid directory: $JAVA_HOME"
else
    JAVACMD=java
    command -v java >/dev/null 2>&1 || die "ERROR: JAVA_HOME is not set and 'java' was not found in PATH."
fi
eval set -- $DEFAULT_JVM_OPTS $JAVA_OPTS $GRADLE_OPTS '"-Dorg.gradle.appname=$APP_BASE_NAME"' -classpath '"$CLASSPATH"' org.gradle.wrapper.GradleWrapperMain '"$@"'
exec "$JAVACMD" "$@"
"""

# Standard Gradle wrapper batch script (Windows). Included for completeness.
_GRADLEW_BAT_TEMPLATE = """\
@rem Gradle startup script for Windows
@if "%DEBUG%"=="" @echo off
@rem Set local scope for the variables with windows NT shell
if "%OS%"=="Windows_NT" setlocal
set DIRNAME=%~dp0
if "%DIRNAME%"=="" set DIRNAME=.
set APP_BASE_NAME=%~n0
set APP_HOME=%DIRNAME%
set DEFAULT_JVM_OPTS="-Xmx64m" "-Xms64m"
set CLASSPATH=%APP_HOME%\\gradle\\wrapper\\gradle-wrapper.jar
@rem Find java.exe
if defined JAVA_HOME goto findJavaFromJavaHome
set JAVA_EXE=java.exe
%JAVA_EXE% -version >NUL 2>&1
if %ERRORLEVEL% equ 0 goto execute
echo ERROR: JAVA_HOME is not set and no 'java' command could be found in your PATH.
goto fail
:findJavaFromJavaHome
set JAVA_HOME=%JAVA_HOME:"=%
set JAVA_EXE=%JAVA_HOME%/bin/java.exe
if exist "%JAVA_EXE%" goto execute
echo ERROR: JAVA_HOME is set to an invalid directory: %JAVA_HOME%
goto fail
:execute
set CMD_LINE_ARGS=
:loop
if %1=="" goto end
set CMD_LINE_ARGS=%CMD_LINE_ARGS% %1
shift
goto loop
:end
"%JAVA_EXE%" %DEFAULT_JVM_OPTS% %JAVA_OPTS% %GRADLE_OPTS% "-Dorg.gradle.appname=%APP_BASE_NAME%" -classpath "%CLASSPATH%" org.gradle.wrapper.GradleWrapperMain %CMD_LINE_ARGS%
:fail
exit /b 1
"""

# gradle-wrapper.properties template. {version} is substituted at seeding time.
_GRADLE_WRAPPER_PROPERTIES_TEMPLATE = """\
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-{version}-bin.zip
networkTimeout=10000
validateDistributionUrl=true
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
"""

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

        if spec.runtime_type == "android_gradle":
            self._seed_gradle_wrapper(driver, session, paths.source_dir)

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

    def _seed_gradle_wrapper(
        self,
        driver: BaseEnvironmentDriver,
        session: EnvironmentSession,
        source_dir: Path,
    ) -> None:
        """Ensure the Gradle wrapper is fully present in source_dir.

        LLM agents cannot produce binary files, so gradle-wrapper.jar must be seeded
        here. Additionally, when gradlew itself is absent (project has no wrapper at all),
        we run `gradle wrapper` inside the container to generate all four files:
          gradlew, gradlew.bat, gradle/wrapper/gradle-wrapper.properties,
          and gradle/wrapper/gradle-wrapper.jar.

        If gradlew already exists but the JAR is missing, we extract the JAR directly
        from the official Gradle distribution ZIP (no gradlew overwrite needed).

        Seeding is skipped when both gradlew and the JAR are already present.
        """
        gradlew_host_path = source_dir / "gradlew"
        jar_host_path = source_dir / "gradle" / "wrapper" / "gradle-wrapper.jar"

        gradlew_present = gradlew_host_path.exists()
        jar_present = jar_host_path.exists() and jar_host_path.stat().st_size > 10_000

        if gradlew_present and jar_present:
            logger.info(
                "environment_bootstrapper_gradle_wrapper_already_seeded project_id=%s",
                session.project_id,
            )
            return

        version = settings.gradle_wrapper_version

        if not gradlew_present:
            # gradlew, gradlew.bat and gradle-wrapper.properties are plain text files —
            # write them directly on the host without involving the container. This avoids
            # any dependency on `gradle` being available in the container's PATH.
            self._write_wrapper_scripts(source_dir, version, session.project_id)
            # Re-check whether the JAR was also written (won't be — we only write text)
            jar_present = jar_host_path.exists() and jar_host_path.stat().st_size > 10_000

        if jar_present:
            return

        # JAR still missing — extract it from the official Gradle distribution ZIP.
        # (gradlew and gradle-wrapper.properties were already written as text above.)
        # The user-facing gradle-wrapper.jar is nested inside the distribution ZIP:
        #   gradle-{v}/lib/plugins/gradle-wrapper-{v}.jar  ← outer plugin JAR
        #     └─ gradle-wrapper.jar                         ← the actual wrapper binary
        dist_url = _GRADLE_DISTRIBUTION_URL_TEMPLATE.format(version=version)

        logger.info(
            "environment_bootstrapper_seeding_gradle_wrapper_jar project_id=%s version=%s",
            session.project_id,
            version,
        )

        tmp = "/tmp/_gw_seed"
        cmd = (
            f"mkdir -p {tmp} && "
            f"curl -sLf -o {tmp}/dist.zip '{dist_url}' && "
            f"unzip -q {tmp}/dist.zip 'gradle-{version}/lib/plugins/gradle-wrapper-{version}.jar' -d {tmp}/e && "
            f"unzip -q {tmp}/e/gradle-{version}/lib/plugins/gradle-wrapper-{version}.jar gradle-wrapper.jar -d {tmp}/i && "
            f"mkdir -p gradle/wrapper && "
            f"mv {tmp}/i/gradle-wrapper.jar gradle/wrapper/gradle-wrapper.jar && "
            f"rm -rf {tmp}"
        )
        result = driver.run_command(session, cmd, source_dir)
        if not result.succeeded:
            driver.stop_session(session)
            self.session_store.remove_session(session.project_id)
            raise EnvironmentBootstrapError(
                f"Gradle wrapper JAR seeding failed for project {session.project_id}.\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}\n"
                f"Ensure the container has internet access to reach: {dist_url}"
            )
        logger.info(
            "environment_bootstrapper_gradle_wrapper_jar_seeded project_id=%s version=%s",
            session.project_id,
            version,
        )

    def _write_wrapper_scripts(
        self, source_dir: Path, version: str, project_id: int
    ) -> None:
        """Write gradlew, gradlew.bat and gradle-wrapper.properties to source_dir.

        These are plain text files so they are written directly on the host without
        running any command inside the container. gradle-wrapper.jar (binary) is
        handled separately by the JAR extraction step.
        """
        wrapper_dir = source_dir / "gradle" / "wrapper"
        wrapper_dir.mkdir(parents=True, exist_ok=True)

        gradlew_path = source_dir / "gradlew"
        gradlew_path.write_text(_GRADLEW_TEMPLATE, encoding="utf-8")
        gradlew_path.chmod(0o755)

        (source_dir / "gradlew.bat").write_text(_GRADLEW_BAT_TEMPLATE, encoding="utf-8")

        (wrapper_dir / "gradle-wrapper.properties").write_text(
            _GRADLE_WRAPPER_PROPERTIES_TEMPLATE.format(version=version),
            encoding="utf-8",
        )

        logger.info(
            "environment_bootstrapper_wrapper_scripts_written project_id=%s version=%s",
            project_id,
            version,
        )

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
