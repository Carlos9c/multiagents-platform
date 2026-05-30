"""
Environment Manager Agent — orchestrator subagent for incremental package installation.

Triggered by the orchestrator when error_diagnosis.fault_side == "environment" or
when overall_error_class == "missing_dependency" with is_recoverable=True.

Responsibilities:
1. Extract the packages to install from the latest error_diagnosis observation
   using an LLM call (package extraction step).
2. Call EnvironmentManager.install() to install the packages.
3. Persist the updated RuntimeSpec to the DB (project.runtime_spec).
4. Add structured observations to the evidence.
5. On terminal failure (needs_user_input, failed, rolled_back) → raise
   SubagentRejectedStepError so the orchestrator rejects and surfaces to manual review.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.contracts import (
    OBSERVATION_TYPE_ERROR_DIAGNOSIS,
    ExecutionRequest,
)
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.services.environment.manager import EnvironmentManager
from app.services.environment.manager_contracts import (
    EnvironmentManagerRequest,
    PackageRequest,
)
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Package extraction LLM schema
# ---------------------------------------------------------------------------

PACKAGE_EXTRACTION_SYSTEM_PROMPT = prompt_loader.get(
    "environment_manager_agent", "package_extraction"
)


class _PackageSpec(BaseModel):
    name: str
    version: str | None = None
    package_type: str = "app"  # "app" | "system"
    reason: str


class _PackageExtractionOutput(BaseModel):
    packages: list[_PackageSpec] = Field(default_factory=list)
    rationale: str = ""


def _build_extraction_user_prompt(
    *,
    request: ExecutionRequest,
    diagnosis: dict,
    step_instructions: str,
) -> str:
    repair_directive = diagnosis.get("repair_directive", "(not provided)")
    error_class = diagnosis.get("overall_error_class", "unknown")
    fault_side = diagnosis.get("fault_side", "environment")
    root_causes = diagnosis.get("root_cause_errors") or []

    root_cause_lines = ""
    if root_causes:
        lines = [
            f"  - {e.get('error_message', '')} (file: {e.get('affected_file', 'unknown')})"
            for e in root_causes[:5]
        ]
        root_cause_lines = "\n".join(lines)

    prompt_loader.validate_builder_inputs(
        "environment_manager_agent",
        "package_extraction",
        {
            "task_title": request.task_title,
            "technical_constraints": request.technical_constraints,
            "error_class": error_class,
            "fault_side": fault_side,
            "repair_directive": repair_directive,
            "root_cause_lines": root_cause_lines,
            "orchestrator_instruction": step_instructions,
        },
    )
    return f"""Task context:
- title: {request.task_title}
- technical_constraints: {request.technical_constraints}

Error diagnosis:
- overall_error_class: {error_class}
- fault_side: {fault_side}
- repair_directive: {repair_directive}

Root cause errors:
{root_cause_lines or "  (none)"}

Orchestrator instruction:
{step_instructions}

Extract the packages that must be installed to resolve this failure.
""".strip()


# ---------------------------------------------------------------------------
# Subagent
# ---------------------------------------------------------------------------


class EnvironmentManagerAgent(BaseSubagent):
    name = "environment_manager_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime

    def execute_step(
        self,
        *,
        db: Session,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        logger.info(
            "environment_manager_agent_started task_id=%s",
            request.task_id,
        )

        # ── Step 1: find the latest error diagnosis ──────────────────────────
        diagnosis = self._latest_error_diagnosis(state)
        if not diagnosis:
            raise SubagentRejectedStepError(
                "environment_manager_agent: no error_diagnosis observation found in evidence. "
                "Cannot determine which packages to install without a diagnosis."
            )

        # ── Step 2: extract packages via LLM ────────────────────────────────
        packages = self._extract_packages(
            request=request,
            diagnosis=diagnosis,
            step_instructions=step.instructions or "",
        )
        if not packages:
            raise SubagentRejectedStepError(
                "environment_manager_agent: could not identify any packages to install "
                f"from error diagnosis (fault_side={diagnosis.get('fault_side')}). "
                "Manual review required."
            )

        # ── Step 3: load current RuntimeSpec ────────────────────────────────
        spec = self._load_runtime_spec(db, request.project_id)

        # ── Step 4: install packages ─────────────────────────────────────────
        mgr_request = EnvironmentManagerRequest(
            packages=packages,
            version_constraint="any_compatible",  # orchestrator path always uses any_compatible
            project_id=request.project_id,
            workspace_path=request.context.workspace_path,
            context_description=f"task_id={request.task_id}: {request.task_title}",
        )

        manager = EnvironmentManager()
        output = manager.install(request=mgr_request, spec=spec)

        logger.info(
            "environment_manager_agent_install_result task_id=%s status=%s installed=%s",
            request.task_id,
            output.status,
            [p.name for p in output.installed_packages],
        )

        # ── Step 5: persist updated RuntimeSpec ──────────────────────────────
        if output.updated_runtime_spec_json:
            self._persist_runtime_spec(
                db,
                request.project_id,
                output.updated_runtime_spec_json,
                state,
            )

        # ── Step 6: add evidence observations ────────────────────────────────
        for obs in output.observations:
            state.evidence.add_note(
                message=obs,
                producer=self.name,
            )
        if output.manifest_changes:
            state.evidence.add_note(
                message=f"Manifest updated: {'; '.join(output.manifest_changes)}",
                producer=self.name,
            )

        # Register manifest files modified on disk (pom.xml, Cargo.toml, go.mod,
        # .csproj, pubspec.yaml, etc.) so validators and downstream observers know
        # which repo files changed during this install.
        for changed_file in output.manifest_files_changed:
            state.evidence.add_changed_file(
                path=changed_file,
                change_type="modified",
                producer=self.name,
            )

        if output.requires_rebuild:
            state.evidence.add_note(
                message=(
                    "Environment rebuild required after package installation "
                    "(JVM/compiled runtime). Run the build step before next verification."
                ),
                producer=self.name,
            )

        # ── Step 7: handle terminal outcomes ─────────────────────────────────
        if output.status in ("needs_user_input", "failed", "rolled_back"):
            blocker = output.blocker_message or f"Package installation {output.status}."
            raise SubagentRejectedStepError(
                f"environment_manager_agent: {blocker} " f"conflicts={output.dependency_conflicts}"
            )

        return state

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _latest_error_diagnosis(self, state: ResolutionState) -> dict | None:
        for item in reversed(state.evidence.observations):
            if item.evidence_type == OBSERVATION_TYPE_ERROR_DIAGNOSIS:
                return item.payload
        return None

    def _extract_packages(
        self,
        *,
        request: ExecutionRequest,
        diagnosis: dict,
        step_instructions: str,
    ) -> list[PackageRequest]:
        user_prompt = _build_extraction_user_prompt(
            request=request,
            diagnosis=diagnosis,
            step_instructions=step_instructions,
        )

        last_exc: ValidationError | None = None
        for attempt in range(2):
            raw = self.runtime.generate_structured(
                system_prompt=PACKAGE_EXTRACTION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="package_extraction_output",
                json_schema=_PackageExtractionOutput.model_json_schema(),
            )
            try:
                parsed = _PackageExtractionOutput.model_validate(raw)
                return [
                    PackageRequest(
                        name=p.name.strip(),
                        version=p.version or None,
                        package_type=(
                            p.package_type if p.package_type in ("app", "system") else "app"
                        ),
                    )
                    for p in parsed.packages
                    if p.name.strip()
                ]
            except ValidationError as exc:
                last_exc = exc
                logger.warning(
                    "env_manager_agent_extraction_invalid attempt=%s error=%s",
                    attempt + 1,
                    str(exc),
                )

        logger.warning(
            "env_manager_agent_extraction_failed final_error=%s",
            str(last_exc),
        )
        return []

    def _load_runtime_spec(self, db: Session, project_id: int):
        """Load RuntimeSpec from the project DB record, or return None."""
        try:
            from app.models.project import Project
            from app.services.environment.contracts import RuntimeSpec

            project = db.get(Project, project_id)
            if project and getattr(project, "runtime_spec", None):
                return RuntimeSpec.model_validate_json(project.runtime_spec)
        except Exception as exc:
            logger.warning(
                "env_manager_agent_spec_load_failed project_id=%s error=%s",
                project_id,
                str(exc),
            )
        return None

    def _persist_runtime_spec(
        self,
        db: Session,
        project_id: int,
        spec_json: str,
        state: ResolutionState,
    ) -> None:
        """Write the updated RuntimeSpec JSON back to project.runtime_spec.

        If persistence fails, the packages were installed in the container but the
        manifest was not updated. This inconsistency is surfaced as an evidence note
        so validators and downstream observers can detect and react to it.
        """
        try:
            from app.models.project import Project

            project = db.get(Project, project_id)
            if project is not None:
                project.runtime_spec = spec_json  # type: ignore[attr-defined]
                db.add(project)
                db.flush()
                logger.info(
                    "env_manager_agent_spec_persisted project_id=%s",
                    project_id,
                )
        except Exception as exc:
            logger.warning(
                "env_manager_agent_spec_persist_failed project_id=%s error=%s",
                project_id,
                str(exc),
            )
            # Surface the failure so it is visible in evidence rather than silently lost.
            # The packages were installed in the container but the manifest is stale.
            state.evidence.add_note(
                message=(
                    f"WARNING: RuntimeSpec manifest update failed after successful package "
                    f"installation (project_id={project_id}): {exc}. "
                    "Installed packages are active in the container but will not survive "
                    "the next environment rebuild. Manual manifest correction may be needed."
                ),
                producer=self.name,
            )
