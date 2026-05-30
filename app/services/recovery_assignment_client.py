from __future__ import annotations

import json

from pydantic import ValidationError

from app.schemas.recovery_assignment import (
    RecoveryAssignmentInput,
    RecoveryAssignmentLLMOutput,
)
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

RECOVERY_ASSIGNMENT_SYSTEM_PROMPT = prompt_loader.get("recovery_assignment")


def _pretty_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)


def build_recovery_assignment_user_prompt(
    assignment_input: RecoveryAssignmentInput,
) -> str:
    payload = assignment_input.model_dump(mode="json")

    prompt_loader.validate_builder_inputs(
        "recovery_assignment",
        "main",
        {
            "assignment_input": assignment_input,
        },
    )
    return f"""
Plan a recovery assignment proposal for the provided live-plan situation.

Important:
- The global action has already been resolved.
- You must not assign final batch ids.
- You must propose clusters and placement relations only.
- Every new task must be covered exactly once.
- The order inside each cluster must be the execution order from earliest to latest.

Structured assignment input:
{_pretty_json(payload)}

Return only JSON matching the requested schema.
""".strip()


def build_recovery_assignment_retry_prompt(
    *,
    validation_error: str,
    assignment_input: RecoveryAssignmentInput,
) -> str:
    payload = assignment_input.model_dump(mode="json")
    prompt_loader.validate_builder_inputs(
        "recovery_assignment",
        "retry",
        {
            "validation_error": validation_error,
            "assignment_input": assignment_input,
        },
    )
    return f"""
Your previous recovery assignment output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Critical reminders:
- Cover every new task exactly once.
- Every task_assessment.suggested_cluster_id must exist in clusters.
- task_assessments and clusters must cover the exact same task ids.
- No task may appear in more than one cluster.
- Cluster task order must be execution order, earliest to latest.
- If a task depends on another new task, both tasks must be in the same cluster and the dependency must appear earlier.
- Cross-cluster new-task dependencies are not allowed in this contract.
- impact_type must match between each task and its cluster.
- Use the resolved assignment mode unless structural conflict truly requires replan.
- Do not assign final batch ids.

Structured assignment input:
{_pretty_json(payload)}

Return only JSON matching the requested schema.
""".strip()


def call_recovery_assignment_model(
    *,
    assignment_input: RecoveryAssignmentInput,
) -> RecoveryAssignmentLLMOutput:
    provider = get_llm_provider()
    first_user_prompt = build_recovery_assignment_user_prompt(
        assignment_input=assignment_input,
    )

    raw = provider.generate_structured(
        system_prompt=RECOVERY_ASSIGNMENT_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="recovery_assignment_output",
        json_schema=RecoveryAssignmentLLMOutput.model_json_schema(),
    )

    try:
        return RecoveryAssignmentLLMOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_recovery_assignment_retry_prompt(
            validation_error=str(exc),
            assignment_input=assignment_input,
        )

        raw_retry = provider.generate_structured(
            system_prompt=RECOVERY_ASSIGNMENT_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="recovery_assignment_output",
            json_schema=RecoveryAssignmentLLMOutput.model_json_schema(),
        )

        return RecoveryAssignmentLLMOutput.model_validate(raw_retry)
