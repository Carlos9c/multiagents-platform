"""QABootstrapper.

Pre-orchestration phase that ensures a testable artifact exists before probing
begins. For most product types this is a no-op. For mobile_android (and future
compiled targets), the bootstrapper:

  1. Scans the project source for an existing APK / compiled artifact.
  2. If found: sets request.artifact_path and returns (request, None).
  3. If not found: uses an LLM to detect the correct build command by reading
     build configuration files (build.gradle, pubspec.yaml, README, etc.).
  4. Executes the build command inside the project's Docker container.
  5. Scans for the newly produced artifact.
  6. If everything succeeds: returns (updated_request, None).
  7. If anything fails: returns (request, QAResult(verdict="blocked", ...)).
"""

from __future__ import annotations

import glob
import logging
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.qa.contracts import QA_VERDICT_BLOCKED, QARequest, QAResult
from app.services.qa.strategies.base import QAStrategy

logger = logging.getLogger(__name__)

# Glob patterns used to locate existing artifacts before attempting a build.
_ARTIFACT_GLOB_PATTERNS: dict[str, list[str]] = {
    "mobile_android": [
        "**/*.apk",
        "**/app/build/outputs/apk/**/*.apk",
        "**/build/outputs/apk/**/*.apk",
    ],
}

# Build configuration files to read for LLM build-command detection.
_BUILD_CONFIG_FILENAMES = {
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "pubspec.yaml",
    "package.json",
    "AndroidManifest.xml",
    "README.md",
    "README.rst",
    "README.txt",
}

# Max characters read from each build config file for the LLM prompt.
_MAX_FILE_CHARS = 3000

# Max number of files listed in the source root for the LLM prompt.
_MAX_FILE_LISTING = 50

# Timeout for the Docker build command in seconds.
_BUILD_TIMEOUT_SECONDS = 600


# ── LLM output schema ─────────────────────────────────────────────────────────


class _BuildDetectionOutput(BaseModel):
    can_detect: bool
    build_command: str | None
    output_pattern: str | None
    reasoning: str


# ── Internal helpers ──────────────────────────────────────────────────────────


def _find_artifact(source_path: str, product_type: str) -> str | None:
    """Return the path to the first existing artifact, or None."""
    patterns = _ARTIFACT_GLOB_PATTERNS.get(product_type, [])
    for pattern in patterns:
        matches = glob.glob(str(Path(source_path) / pattern), recursive=True)
        if matches:
            return matches[0]
    return None


def _read_build_configs(source_path: str) -> dict[str, str]:
    """Read relevant build configuration files from the source tree."""
    root = Path(source_path)
    contents: dict[str, str] = {}
    for fname in _BUILD_CONFIG_FILENAMES:
        # Search up to two directory levels deep
        for path in list(root.rglob(fname))[:3]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                rel = str(path.relative_to(root))
                contents[rel] = text[:_MAX_FILE_CHARS]
            except OSError:
                continue
    return contents


def _list_source_files(source_path: str) -> list[str]:
    """List files in the source root (shallow, for the LLM prompt)."""
    root = Path(source_path)
    try:
        return sorted(
            str(p.relative_to(root)) for p in root.iterdir() if not p.name.startswith(".")
        )[:_MAX_FILE_LISTING]
    except OSError:
        return []


def _detect_build_command(
    request: QARequest,
) -> _BuildDetectionOutput | None:
    """Use LLM to detect the build command for the project."""
    source_path = request.source_path
    file_listing = _list_source_files(source_path)
    build_configs = _read_build_configs(source_path)
    build_file_contents = "\n\n".join(
        f"=== {fname} ===\n{content}" for fname, content in build_configs.items()
    )

    system_prompt = prompt_loader.get("qa/qa_bootstrapper", "detect_build")
    prompt_loader.validate_builder_inputs(
        "qa/qa_bootstrapper",
        "detect_build",
        {
            "product_type": request.product_type,
            "source_path": source_path,
            "file_listing": file_listing,
            "build_file_contents": build_file_contents or "(none found)",
        },
    )

    user_prompt = f"""product_type: {request.product_type}
source_path: {source_path}
file_listing: {file_listing}

build_file_contents:
{build_file_contents or "(none found)"}

Detect the build command that will produce the deployable artifact."""

    try:
        provider = get_llm_provider()
        raw = provider.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name="build_detection_output",
            json_schema=_BuildDetectionOutput.model_json_schema(),
        )
        return _BuildDetectionOutput.model_validate(raw)
    except Exception as exc:
        logger.warning("qa_bootstrapper_llm_detection_failed exc=%s", exc)
        return None


def _run_build_in_container(
    request: QARequest,
    build_command: str,
) -> tuple[bool, str]:
    """Run the build command inside the project's Docker container.

    Returns (success, error_message). Override in tests.
    """
    try:
        from app.services.environment.registry import get_driver_registry
        from app.services.environment.session_store import get_session_store

        store = get_session_store()
        session = store.get_session(request.project_id)
        if session is None:
            return False, "No active Docker session for this project"

        registry = get_driver_registry()
        driver = registry.get_driver(session.runtime_type)
        result = driver.run_command(
            session=session,
            command=build_command,
            cwd=Path(request.source_path),
            timeout_seconds=_BUILD_TIMEOUT_SECONDS,
        )

        if result.exit_code != 0:
            output = f"exit_code={result.exit_code}\n{result.stderr or result.stdout}"
            return False, f"Build command failed: {output[:1000]}"

        return True, ""

    except Exception as exc:
        logger.warning("qa_bootstrapper_docker_build_failed exc=%s", exc)
        return False, str(exc)


# ── Public API ────────────────────────────────────────────────────────────────


class QABootstrapper:
    """Ensures a testable artifact exists before QA probing begins.

    Returns (updated_request, None) on success, or (request, blocked_result) on failure.
    """

    def bootstrap(
        self,
        *,
        db: Session,
        request: QARequest,
        strategy: QAStrategy,
    ) -> tuple[QARequest, QAResult | None]:
        if not strategy.requires_compiled_artifact:
            return request, None

        logger.info(
            "qa_bootstrapper_start project_id=%s product_type=%s",
            request.project_id,
            request.product_type,
        )

        # Step 1: Check for existing artifact
        existing = _find_artifact(request.source_path, request.product_type)
        if existing:
            logger.info("qa_bootstrapper_artifact_found path=%s", existing)
            return request.model_copy(update={"artifact_path": existing}), None

        # Step 2: Detect build command via LLM
        build_info = _detect_build_command(request)
        if build_info is None or not build_info.can_detect:
            reason = build_info.reasoning if build_info is not None else "LLM call failed"
            logger.warning("qa_bootstrapper_cannot_detect_build reason=%s", reason)
            return request, QAResult(
                verdict=QA_VERDICT_BLOCKED,
                error_message=f"Could not detect build command: {reason}",
            )

        build_command = build_info.build_command or ""
        logger.info("qa_bootstrapper_running_build command=%s", build_command)

        # Step 3: Execute build in Docker
        success, error = _run_build_in_container(request, build_command)
        if not success:
            logger.warning("qa_bootstrapper_build_failed error=%s", error)
            return request, QAResult(
                verdict=QA_VERDICT_BLOCKED,
                error_message=f"APK compilation failed: {error}",
            )

        # Step 4: Locate the produced artifact
        artifact = _find_artifact(request.source_path, request.product_type)
        if artifact is None and build_info.output_pattern:
            # Try the LLM-suggested pattern directly
            matches = glob.glob(
                str(Path(request.source_path) / build_info.output_pattern),
                recursive=True,
            )
            artifact = matches[0] if matches else None

        if artifact is None:
            return request, QAResult(
                verdict=QA_VERDICT_BLOCKED,
                error_message="Build succeeded but no artifact was found at the expected location",
            )

        logger.info("qa_bootstrapper_artifact_produced path=%s", artifact)
        return request.model_copy(update={"artifact_path": artifact}), None
