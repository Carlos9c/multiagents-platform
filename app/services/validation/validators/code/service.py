from __future__ import annotations

from pydantic import ValidationError

from app.models.task import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema
from app.services.validation.contracts import (
    TaskValidationInput,
    ValidationFinding,
    ValidationResult,
)
from app.services.validation.validators.code.prompt import (
    CODE_TASK_VALIDATOR_SYSTEM_PROMPT,
    build_code_task_validator_user_prompt,
)
from app.services.validation.validators.code.renderer import (
    render_code_validation_evidence,
)
from app.services.validation.validators.code.schemas import CodeValidationLLMOutput


class CodeTaskValidatorError(Exception):
    """Base exception for code-task validation failures."""


def _map_decision_to_final_task_status(decision: str) -> str | None:
    if decision == "completed":
        return TASK_STATUS_COMPLETED
    if decision == "partial":
        return TASK_STATUS_PARTIAL
    if decision in {"failed", "manual_review"}:
        return TASK_STATUS_FAILED
    return None


def _recommend_followup_validators(
    validation_input: TaskValidationInput, unconsumed_ids: list[str]
) -> list[str]:
    if not unconsumed_ids:
        return []

    recommended: list[str] = []
    for item in validation_input.evidence_package.evidence_items:
        if item.evidence_id not in unconsumed_ids:
            continue
        if item.media_type and item.media_type.startswith("image/"):
            if "image_task_validator" not in recommended:
                recommended.append("image_task_validator")
        elif item.media_type and item.media_type.startswith("audio/"):
            if "audio_task_validator" not in recommended:
                recommended.append("audio_task_validator")
        elif item.media_type in {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }:
            if "document_task_validator" not in recommended:
                recommended.append("document_task_validator")

    return recommended


def validate_code_task_with_llm(
    *,
    validation_input: TaskValidationInput,
) -> ValidationResult:
    renderable_evidence = render_code_validation_evidence(
        validation_input=validation_input,
    )

    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        CodeValidationLLMOutput.model_json_schema()
    )
    user_prompt = build_code_task_validator_user_prompt(
        validation_input=validation_input,
        renderable_evidence=renderable_evidence,
    )

    raw = provider.generate_structured(
        system_prompt=CODE_TASK_VALIDATOR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="code_validation_llm_output",
        json_schema=strict_schema,
    )

    try:
        llm_output = CodeValidationLLMOutput.model_validate(raw)
    except ValidationError as exc:
        raise CodeTaskValidatorError(
            f"Code validator returned structurally invalid output: {str(exc)}"
        ) from exc

    validated_evidence_ids = [
        item.evidence_id for item in renderable_evidence.supported_items
    ]
    unconsumed_evidence_ids = [
        item.evidence_id for item in renderable_evidence.unsupported_items
    ]

    followup_validation_required = bool(unconsumed_evidence_ids)
    recommended_next_validator_keys = _recommend_followup_validators(
        validation_input=validation_input,
        unconsumed_ids=unconsumed_evidence_ids,
    )

    partial_validation_summary = None
    if followup_validation_required:
        partial_validation_summary = (
            "This validator consumed only the evidence formats it supports. "
            "Additional validation may be required for unconsumed evidence."
        )

    return ValidationResult(
        validator_key=validation_input.intent.validator_key,
        discipline=validation_input.intent.discipline,
        decision=llm_output.decision,
        summary=llm_output.summary,
        findings=[
            ValidationFinding(
                severity=finding.severity,
                message=finding.message,
                code=finding.category,
                file_path=finding.file_paths[0] if finding.file_paths else None,
            )
            for finding in llm_output.findings
        ],
        validated_scope=llm_output.validated_scope,
        missing_scope=llm_output.missing_scope,
        blockers=list(llm_output.blockers),
        manual_review_required=llm_output.manual_review_required,
        final_task_status=_map_decision_to_final_task_status(llm_output.decision),
        artifacts_created=[],
        validated_evidence_ids=validated_evidence_ids,
        unconsumed_evidence_ids=unconsumed_evidence_ids,
        followup_validation_required=followup_validation_required,
        recommended_next_validator_keys=recommended_next_validator_keys,
        partial_validation_summary=partial_validation_summary,
        metadata={
            "confidence": llm_output.confidence,
            "reasoning_notes": list(llm_output.reasoning_notes),
            "raw_findings_count": len(llm_output.findings),
            "supported_evidence_count": len(validated_evidence_ids),
            "unsupported_evidence_count": len(unconsumed_evidence_ids),
        },
    )
