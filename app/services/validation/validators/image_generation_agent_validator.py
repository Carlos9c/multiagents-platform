from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.execution_engine.contracts import (
    OBSERVATION_TYPE_IMAGE_GENERATED,
    EvidenceItem,
)
from app.models.task import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
)
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.validation.base import BaseTaskValidator
from app.services.validation.contracts import (
    TaskValidationInput,
    ValidationFinding,
    ValidationResult,
)
from app.services.validation.helpers.evidence import build_producer_evidence_view
from app.services.validation.helpers.resources import read_binary_from_context

logger = logging.getLogger(__name__)

VALIDATOR_KEY = "image_generation_agent_validator"
PRODUCER_KEY = "image_generation_agent"

IMAGE_GENERATION_AGENT_VALIDATOR_SYSTEM_PROMPT = prompt_loader.get(
    "image_generation_agent_validator"
)

_SUPPORTED_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


# ── LLM output schema ──────────────────────────────────────────────────────


class ImageValidationFinding(BaseModel):
    severity: Literal["info", "warning", "error"]
    category: str = Field(..., min_length=3)
    message: str


class ImageValidationLLMOutput(BaseModel):
    decision: Literal["completed", "partial", "failed", "manual_review"]
    summary: str
    validated_scope: str | None = None
    missing_scope: str | None = None
    blockers: list[str] = Field(default_factory=list)
    findings: list[ImageValidationFinding] = Field(default_factory=list)
    manual_review_required: bool = False


# ── Helpers ────────────────────────────────────────────────────────────────


def _map_decision_to_final_task_status(decision: str) -> str | None:
    if decision == "completed":
        return TASK_STATUS_COMPLETED
    if decision == "partial":
        return TASK_STATUS_PARTIAL
    if decision in {"failed", "manual_review"}:
        return TASK_STATUS_FAILED
    return None


def _extract_image_observation(items: list[EvidenceItem]) -> EvidenceItem | None:
    for item in items:
        if item.evidence_type == OBSERVATION_TYPE_IMAGE_GENERATED:
            return item
    return None


def _extract_image_path_from_changed_files(items: list[EvidenceItem]) -> str | None:
    for item in items:
        if item.evidence_type == "changed_file":
            path = item.path or item.payload.get("path", "")
            if Path(path).suffix.lower() in _SUPPORTED_IMAGE_EXTENSIONS:
                return path
    return None


def _format_generation_metadata(observation: EvidenceItem | None) -> str:
    if observation is None:
        return "[no generation metadata available]"
    payload = observation.payload or {}
    pe = payload.get("prompt_engineering", {})
    gen = payload.get("generation", {})
    out = payload.get("output", {})
    return (
        f"style_directive: {pe.get('style_directive', 'N/A')}\n"
        f"design_rationale: {pe.get('design_rationale', 'N/A')}\n"
        f"intended_colors: {pe.get('intended_colors', [])}\n"
        f"main_prompt: {pe.get('main_prompt', 'N/A')[:400]}\n"
        f"model: {gen.get('model', 'N/A')}\n"
        f"was_resized: {out.get('was_resized', False)}\n"
        f"variants: {out.get('variants', [])}"
    )


def _build_technical_failure(
    *,
    validator_key: str,
    message: str,
) -> ValidationResult:
    return ValidationResult(
        validator_key=validator_key,
        discipline="image",
        decision="failed",
        summary=message,
        findings=[ValidationFinding(severity="error", message=message)],
        final_task_status=TASK_STATUS_FAILED,
    )


# ── Validator ──────────────────────────────────────────────────────────────


class ImageGenerationAgentValidator(BaseTaskValidator):
    validator_key = VALIDATOR_KEY
    producer_key = PRODUCER_KEY

    def validate(self, validation_input: TaskValidationInput) -> ValidationResult:
        request = validation_input.execution_request
        result = validation_input.execution_result

        evidence_view = build_producer_evidence_view(result.evidence, producer=PRODUCER_KEY)

        if not evidence_view.has_any():
            return _build_technical_failure(
                validator_key=self.validator_key,
                message="No evidence found for image_generation_agent — agent may not have run.",
            )

        # Identify the primary image path
        observation = _extract_image_observation(evidence_view.items)
        if observation and observation.path:
            image_path = observation.path
        else:
            image_path = _extract_image_path_from_changed_files(evidence_view.items)

        if not image_path:
            return _build_technical_failure(
                validator_key=self.validator_key,
                message="Could not identify the generated image path from evidence.",
            )

        # ── Technical validation (no LLM) ──────────────────────────────────
        binary = read_binary_from_context(request, logical_path=image_path)

        if not binary.exists or binary.content is None:
            return _build_technical_failure(
                validator_key=self.validator_key,
                message=f"Generated image not found at '{image_path}': {binary.error}",
            )

        image_bytes = binary.content
        if len(image_bytes) < 100:
            return _build_technical_failure(
                validator_key=self.validator_key,
                message=f"Generated image at '{image_path}' is suspiciously small ({len(image_bytes)} bytes).",
            )

        # Determine dimensions from observation payload
        payload = observation.payload if observation else {}
        output_meta = payload.get("output", {})
        final_w = output_meta.get("final_width", "?")
        final_h = output_meta.get("final_height", "?")
        image_format = output_meta.get("format", Path(image_path).suffix.lstrip(".")).upper()
        image_dimensions = f"{final_w}×{final_h}px"

        logger.info(
            "image_generation_validator_technical_ok task_id=%s path=%s size_bytes=%d",
            request.task_id,
            image_path,
            len(image_bytes),
        )

        # ── Semantic validation (vision LLM) ───────────────────────────────
        from app.core.config import settings

        provider = get_llm_provider(
            model=settings.validator_model,
            provider=settings.validator_provider,
        )

        generation_metadata = _format_generation_metadata(observation)

        from app.services.prompt_loader import prompt_loader as _pl

        _pl.validate_builder_inputs(
            "image_generation_agent_validator",
            "main",
            {
                "task_title": request.task_title,
                "task_description": request.task_description,
                "acceptance_criteria": request.acceptance_criteria,
                "image_path": image_path,
                "image_dimensions": image_dimensions,
                "image_format": image_format,
                "generation_metadata": generation_metadata,
            },
        )

        user_prompt = f"""Task title: {request.task_title}

Task description: {request.task_description or 'N/A'}

Acceptance criteria: {request.acceptance_criteria or 'N/A'}

Image path: {image_path}
Image dimensions: {image_dimensions}
Image format: {image_format}

Generation metadata:
{generation_metadata}

The image is attached above. Please evaluate it against the task requirements.
""".strip()

        try:
            raw: dict[str, Any] = provider.generate_structured(
                system_prompt=IMAGE_GENERATION_AGENT_VALIDATOR_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="image_validation_output",
                json_schema=ImageValidationLLMOutput.model_json_schema(),
                images=[image_bytes],
            )
            llm_output = ImageValidationLLMOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning(
                "image_generation_validator_llm_failed task_id=%s error=%s",
                request.task_id,
                str(exc),
            )
            return ValidationResult(
                validator_key=self.validator_key,
                discipline="image",
                decision="manual_review",
                summary=f"Vision LLM validation failed: {str(exc)[:200]}",
                manual_review_required=True,
                final_task_status=TASK_STATUS_FAILED,
            )

        findings = [
            ValidationFinding(
                severity=f.severity,
                message=f.message,
                code=f.category,
                file_path=image_path,
            )
            for f in llm_output.findings
        ]

        logger.info(
            "image_generation_validator_completed task_id=%s decision=%s",
            request.task_id,
            llm_output.decision,
        )

        return ValidationResult(
            validator_key=self.validator_key,
            discipline="image",
            decision=llm_output.decision,
            summary=llm_output.summary,
            findings=findings,
            validated_scope=llm_output.validated_scope,
            missing_scope=llm_output.missing_scope,
            blockers=llm_output.blockers,
            manual_review_required=llm_output.manual_review_required,
            final_task_status=_map_decision_to_final_task_status(llm_output.decision),
        )
