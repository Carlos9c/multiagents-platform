"""
Shared helpers for the recovery/evaluation layer supervisor evaluators.

Used by:
  - StageEvaluatorEvaluator       (evaluation_decision artifacts)
  - RecoveryPlannerEvaluator      (recovery_decision artifacts + ExecutionRun/Task cross-context)
  - RecoveryAssignmentEvaluator   (trios of recovery_assignment_input/output/compiled_plan artifacts)

All three agents store their outputs directly in DB Artifacts, so no execution_trace.jsonl
is involved here — data access is a plain DB query.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.task import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Artifact type constants
# ---------------------------------------------------------------------------

_EVALUATION_DECISION_TYPE = "evaluation_decision"
_RECOVERY_DECISION_TYPE = "recovery_decision"
_RECOVERY_ASSIGNMENT_INPUT_TYPE = "recovery_assignment_input"
_RECOVERY_ASSIGNMENT_OUTPUT_TYPE = "recovery_assignment_output"
_RECOVERY_ASSIGNMENT_COMPILED_TYPE = "recovery_assignment_compiled_plan"

_MAX_EVALUATION_ARTIFACTS = 8
_MAX_RECOVERY_DECISION_ARTIFACTS = 10
_MAX_RECOVERY_ASSIGNMENT_TRIOS = 5


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _parse_artifact_content(artifact: Artifact) -> dict[str, Any] | None:
    """Parse artifact JSON content, returning None on failure."""
    try:
        payload = json.loads(artifact.content)
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "_recovery_evaluation_helpers: invalid JSON in artifact id=%s type=%s",
            artifact.id,
            artifact.artifact_type,
        )
    return None


def _query_artifacts_by_type(
    db: Session,
    project_id: int,
    artifact_type: str,
) -> list[Artifact]:
    """Return all artifacts of given type for a project, ordered by id ascending."""
    return (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.artifact_type == artifact_type,
        )
        .order_by(Artifact.id.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# StageEvaluatorEvaluator helpers
# ---------------------------------------------------------------------------


def load_evaluation_decision_artifacts(
    db: Session,
    project_id: int,
    *,
    max_artifacts: int = _MAX_EVALUATION_ARTIFACTS,
) -> list[dict[str, Any]]:
    """Return parsed evaluation_decision artifact payloads for a project.

    Each returned dict is the artifact JSON content augmented with artifact_id.
    Ordered by artifact.id ascending (chronological order of checkpoint evaluations).
    Capped at max_artifacts.
    """
    artifacts = _query_artifacts_by_type(db, project_id, _EVALUATION_DECISION_TYPE)
    results: list[dict[str, Any]] = []
    for artifact in artifacts[:max_artifacts]:
        payload = _parse_artifact_content(artifact)
        if payload is None:
            continue
        payload["artifact_id"] = artifact.id
        results.append(payload)
    return results


# ---------------------------------------------------------------------------
# RecoveryPlannerEvaluator helpers
# ---------------------------------------------------------------------------


def _build_source_run_context(run: ExecutionRun) -> dict[str, Any]:
    """Extract the cross-context fields from a source ExecutionRun."""
    return {
        "run_id": run.id,
        "status": run.status,
        "failure_type": run.failure_type,
        "failure_code": run.failure_code,
        "work_summary": run.work_summary,
        "blockers_found": run.blockers_found,
        "validation_notes": run.validation_notes,
    }


def _build_source_task_context(task: Task) -> dict[str, Any]:
    """Extract the cross-context fields from a source Task."""
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "objective": task.objective,
        "acceptance_criteria": task.acceptance_criteria,
        "status": task.status,
    }


def load_recovery_decision_artifacts(
    db: Session,
    project_id: int,
    *,
    max_artifacts: int = _MAX_RECOVERY_DECISION_ARTIFACTS,
) -> list[dict[str, Any]]:
    """Return recovery_decision artifact payloads enriched with cross-context.

    For each artifact, adds:
      source_run_context  — failure_type, failure_code, work_summary, blockers_found,
                            validation_notes from the source ExecutionRun
      source_task_context — task_type, objective, acceptance_criteria, status from
                            the source Task

    Ordered by artifact.id ascending (chronological).
    Capped at max_artifacts.
    """
    artifacts = _query_artifacts_by_type(db, project_id, _RECOVERY_DECISION_TYPE)
    results: list[dict[str, Any]] = []

    for artifact in artifacts[:max_artifacts]:
        payload = _parse_artifact_content(artifact)
        if payload is None:
            continue

        payload["artifact_id"] = artifact.id

        # Cross-context: source ExecutionRun
        source_run_id = payload.get("source_run_id")
        if source_run_id:
            run = db.get(ExecutionRun, source_run_id)
            payload["source_run_context"] = _build_source_run_context(run) if run else None
        else:
            payload["source_run_context"] = None

        # Cross-context: source Task (artifact.task_id == source_task_id)
        source_task_id = artifact.task_id or payload.get("source_task_id")
        if source_task_id:
            task = db.get(Task, source_task_id)
            payload["source_task_context"] = _build_source_task_context(task) if task else None
        else:
            payload["source_task_context"] = None

        results.append(payload)

    return results


# ---------------------------------------------------------------------------
# RecoveryAssignmentEvaluator helpers
# ---------------------------------------------------------------------------

_TRIO_TYPES_IN_ORDER = (
    _RECOVERY_ASSIGNMENT_INPUT_TYPE,
    _RECOVERY_ASSIGNMENT_OUTPUT_TYPE,
    _RECOVERY_ASSIGNMENT_COMPILED_TYPE,
)


def load_recovery_assignment_trios(
    db: Session,
    project_id: int,
    *,
    max_trios: int = _MAX_RECOVERY_ASSIGNMENT_TRIOS,
) -> list[dict[str, Any]]:
    """Return recovery assignment trios grouped by sequential position.

    Each trio is assembled from three consecutive artifacts (input → output →
    compiled_plan) that are always persisted in that fixed order by
    live_plan_mutation_service.py in a single synchronous call.

    Strategy: collect all three artifact types ordered by artifact.id, then
    group them positionally — index 0,1,2 → trio 0; index 3,4,5 → trio 1; etc.
    We verify the order matches the expected type sequence for each trio.

    Each returned dict has keys: trio_index, input, output, compiled_plan.
    Capped at max_trios.
    """
    # Collect all three types ordered by id
    all_artifacts: list[Artifact] = (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.artifact_type.in_(list(_TRIO_TYPES_IN_ORDER)),
        )
        .order_by(Artifact.id.asc())
        .all()
    )

    # Group into chunks of 3
    trios: list[dict[str, Any]] = []
    chunk_size = len(_TRIO_TYPES_IN_ORDER)

    for trio_index in range(0, len(all_artifacts), chunk_size):
        if len(trios) >= max_trios:
            break

        chunk = all_artifacts[trio_index : trio_index + chunk_size]
        if len(chunk) < chunk_size:
            # Incomplete trio at the end — skip gracefully
            logger.debug(
                "load_recovery_assignment_trios: incomplete trio at index %d "
                "for project %d (got %d of %d artifacts)",
                trio_index,
                project_id,
                len(chunk),
                chunk_size,
            )
            break

        # Verify order matches expected type sequence
        expected_types = list(_TRIO_TYPES_IN_ORDER)
        actual_types = [a.artifact_type for a in chunk]
        if actual_types != expected_types:
            logger.warning(
                "load_recovery_assignment_trios: unexpected artifact order at "
                "trio_index=%d for project %d: expected=%s got=%s",
                trio_index,
                project_id,
                expected_types,
                actual_types,
            )
            # Best-effort: try to assemble by type name anyway
            by_type: dict[str, Artifact] = {}
            for artifact in chunk:
                by_type[artifact.artifact_type] = artifact
        else:
            by_type = {a.artifact_type: a for a in chunk}

        input_artifact = by_type.get(_RECOVERY_ASSIGNMENT_INPUT_TYPE)
        output_artifact = by_type.get(_RECOVERY_ASSIGNMENT_OUTPUT_TYPE)
        compiled_artifact = by_type.get(_RECOVERY_ASSIGNMENT_COMPILED_TYPE)

        if not (input_artifact and output_artifact and compiled_artifact):
            logger.warning(
                "load_recovery_assignment_trios: missing artifact type in trio %d "
                "for project %d, skipping",
                trio_index,
                project_id,
            )
            continue

        trio: dict[str, Any] = {
            "trio_index": len(trios),
            "input": _parse_artifact_content(input_artifact),
            "output": _parse_artifact_content(output_artifact),
            "compiled_plan": _parse_artifact_content(compiled_artifact),
        }
        trios.append(trio)

    return trios
