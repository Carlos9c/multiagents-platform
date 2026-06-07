from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.contracts import (
    CHANGE_TYPE_CREATED,
    OBSERVATION_TYPE_IMAGE_GENERATED,
    ExecutionRequest,
)
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.tools.generate_image_tool import generate_image
from app.execution_engine.tools.resize_image_tool import resize_image
from app.execution_engine.tools.write_binary_file_tool import write_binary_file
from app.services.image_providers.contracts import ImageGenerationRequest
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

IMAGE_GENERATION_AGENT_SYSTEM_PROMPT = prompt_loader.get(
    "image_generation_agent", "prompt_engineering"
)

# ── Output schemas ─────────────────────────────────────────────────────────


class ResizeVariant(BaseModel):
    output_path: str
    width: int
    height: int
    resize_mode: Literal["fit", "fill", "crop"] = "fit"


class ImagePromptEngineeringOutput(BaseModel):
    main_prompt: str
    negative_prompt: str
    style_directive: str
    design_rationale: str
    intended_colors: list[str] = Field(default_factory=list)
    output_format: Literal["png", "jpg", "webp"] = "png"
    target_width: int
    target_height: int
    generation_width: int
    generation_height: int
    needs_resize: bool
    resize_mode: Literal["fit", "fill", "crop"] = "fit"
    output_path: str
    resize_variants: list[ResizeVariant] = Field(default_factory=list)


# ── User prompt builder ────────────────────────────────────────────────────


def _build_prompt_engineering_user_prompt(
    request: ExecutionRequest,
    step: ExecutionStep,
) -> str:
    style_references = _collect_style_references(request)
    project_context = _build_project_context_summary(request)

    prompt_loader.validate_builder_inputs(
        "image_generation_agent",
        "prompt_engineering",
        {
            "task_title": request.task_title,
            "task_description": request.task_description,
            "acceptance_criteria": request.acceptance_criteria,
            "style_references": style_references,
            "project_context_summary": project_context,
            "intended_output_path_hint": request.implementation_notes,
        },
    )

    return f"""Task title: {request.task_title}

Task description: {request.task_description or 'N/A'}

Acceptance criteria: {request.acceptance_criteria or 'N/A'}

Project context:
{project_context}

Style references from previous image tasks:
{style_references or '[none available — this may be the first image in the project]'}

Output path hint: {request.implementation_notes or '[no hint — decide based on context]'}
""".strip()


def _collect_style_references(request: ExecutionRequest) -> str | None:
    """Extract style reference text from historical image tasks."""
    if request.historical_context is None:
        return None

    parts: list[str] = []
    for item in request.historical_context.selected_task_runs:
        image_files = [
            f
            for f in item.changed_files
            if any(f.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
        ]
        if image_files:
            summary = item.run_summary or item.completed_scope or ""
            parts.append(
                f"- Task {item.task_id} ({item.title}): generated {image_files}. " f"{summary}"
            )

    return "\n".join(parts) if parts else None


def _build_project_context_summary(request: ExecutionRequest) -> str:
    related = [
        f"  - task_id={t.task_id} status={t.status}: {t.title}"
        for t in request.context.related_tasks
    ]
    decisions = [f"  - {d}" for d in request.context.key_decisions]
    lines = [
        f"workspace_path: {request.context.workspace_path}",
        f"source_path: {request.context.source_path}",
    ]
    if decisions:
        lines.append("key_decisions:\n" + "\n".join(decisions))
    if related:
        lines.append("related_tasks:\n" + "\n".join(related))
    return "\n".join(lines)


# ── Evidence helpers ───────────────────────────────────────────────────────


def _build_file_documentation(
    *,
    engineering_output: ImagePromptEngineeringOutput,
    actual_width: int,
    actual_height: int,
    model_used: str,
    is_source_original: bool = False,
) -> str:
    colors = (
        ", ".join(engineering_output.intended_colors)
        if engineering_output.intended_colors
        else "N/A"
    )
    dims = f"{actual_width}×{actual_height}px"
    fmt = engineering_output.output_format.upper()

    if is_source_original:
        label = f"Source original {dims} {fmt} (pre-resize)"
    else:
        label = f"{dims} {fmt} image"

    return (
        f"{label}. "
        f"Use case: {engineering_output.style_directive}. "
        f"Colors: {colors}. "
        f"Design rationale: {engineering_output.design_rationale} "
        f"Generation prompt: {engineering_output.main_prompt[:300]}{'...' if len(engineering_output.main_prompt) > 300 else ''}. "
        f"Model: {model_used}."
    )


def _derive_source_path(output_path: str) -> str:
    """Derive a companion path for the high-res source original (pre-resize)."""
    p = Path(output_path)
    return (p.parent / (p.stem + "_source" + p.suffix)).as_posix()


# ── Subagent ───────────────────────────────────────────────────────────────


class ImageGenerationAgent(BaseSubagent):
    name = "image_generation_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self._runtime = runtime

    def execute_step(
        self,
        *,
        db: Session,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        if step.subagent_name != self.name:
            raise SubagentRejectedStepError(
                f"{self.name} received a step for subagent '{step.subagent_name}'."
            )

        state.increment_materialization_attempts()

        logger.info(
            "image_generation_agent_started task_id=%s step_id=%s attempt=%s",
            request.task_id,
            step.id,
            state.materialization_attempt_count,
        )

        # ── Step 1: Prompt engineering ─────────────────────────────────────
        user_prompt = _build_prompt_engineering_user_prompt(request, step)

        raw = self._runtime.generate_structured(
            system_prompt=IMAGE_GENERATION_AGENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="image_prompt_engineering_output",
            json_schema=ImagePromptEngineeringOutput.model_json_schema(),
        )

        try:
            engineering = ImagePromptEngineeringOutput.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "image_generation_agent_invalid_prompt_engineering task_id=%s error=%s",
                request.task_id,
                str(exc),
            )
            raise SubagentRejectedStepError(
                f"Image prompt engineering produced invalid output: {str(exc)}"
            ) from exc

        logger.info(
            "image_generation_agent_prompt_engineered task_id=%s output_path=%s style=%s",
            request.task_id,
            engineering.output_path,
            engineering.style_directive,
        )

        # ── Step 2: Image generation ───────────────────────────────────────
        gen_request = ImageGenerationRequest(
            main_prompt=engineering.main_prompt,
            negative_prompt=engineering.negative_prompt or None,
            style_directive=engineering.style_directive,
            width=engineering.generation_width,
            height=engineering.generation_height,
            output_format=engineering.output_format,
        )

        try:
            gen_result = generate_image(gen_request)
        except Exception as exc:
            logger.exception(
                "image_generation_agent_generation_failed task_id=%s error=%s",
                request.task_id,
                str(exc),
            )
            raise SubagentRejectedStepError(
                f"Image generation API call failed: {str(exc)}"
            ) from exc

        logger.info(
            "image_generation_agent_generated task_id=%s model=%s size=%dx%d bytes=%d",
            request.task_id,
            gen_result.model_used,
            gen_result.actual_width,
            gen_result.actual_height,
            len(gen_result.image_bytes),
        )

        workspace_root = request.context.workspace_path

        # ── Step 3: Resize principal + save source original ────────────────
        if engineering.needs_resize:
            # Save the high-res source original for future variation tasks
            source_path = _derive_source_path(engineering.output_path)
            write_binary_file(
                root_dir=workspace_root,
                relative_path=source_path,
                content=gen_result.image_bytes,
            )
            state.evidence.add_changed_file(
                path=source_path,
                change_type=CHANGE_TYPE_CREATED,
                producer=self.name,
            )
            state.evidence.add_file_documentation(
                path=source_path,
                documentation=_build_file_documentation(
                    engineering_output=engineering,
                    actual_width=gen_result.actual_width,
                    actual_height=gen_result.actual_height,
                    model_used=gen_result.model_used,
                    is_source_original=True,
                ),
                change_summary=f"High-res source original for {engineering.output_path}",
                agent=self.name,
                operation="create",
            )

            final_bytes = resize_image(
                source_bytes=gen_result.image_bytes,
                width=engineering.target_width,
                height=engineering.target_height,
                mode=engineering.resize_mode,
                output_format=engineering.output_format,
            )
        else:
            final_bytes = gen_result.image_bytes

        # ── Write primary output ───────────────────────────────────────────
        write_binary_file(
            root_dir=workspace_root,
            relative_path=engineering.output_path,
            content=final_bytes,
        )

        final_w = engineering.target_width if engineering.needs_resize else gen_result.actual_width
        final_h = (
            engineering.target_height if engineering.needs_resize else gen_result.actual_height
        )

        state.evidence.add_changed_file(
            path=engineering.output_path,
            change_type=CHANGE_TYPE_CREATED,
            producer=self.name,
        )
        state.evidence.add_file_documentation(
            path=engineering.output_path,
            documentation=_build_file_documentation(
                engineering_output=engineering,
                actual_width=final_w,
                actual_height=final_h,
                model_used=gen_result.model_used,
            ),
            change_summary=(
                f"Generated {final_w}×{final_h}px {engineering.output_format.upper()} "
                f"— {engineering.style_directive}"
            ),
            agent=self.name,
            operation="create",
        )

        # ── Step 4: Resize variants ────────────────────────────────────────
        source_for_variants = gen_result.image_bytes  # always resize from high-res original
        for variant in engineering.resize_variants:
            variant_bytes = resize_image(
                source_bytes=source_for_variants,
                width=variant.width,
                height=variant.height,
                mode=variant.resize_mode,
                output_format=engineering.output_format,
            )
            write_binary_file(
                root_dir=workspace_root,
                relative_path=variant.output_path,
                content=variant_bytes,
            )
            state.evidence.add_changed_file(
                path=variant.output_path,
                change_type=CHANGE_TYPE_CREATED,
                producer=self.name,
            )
            colors = (
                ", ".join(engineering.intended_colors) if engineering.intended_colors else "N/A"
            )
            state.evidence.add_file_documentation(
                path=variant.output_path,
                documentation=(
                    f"{variant.width}×{variant.height}px {engineering.output_format.upper()} "
                    f"resize variant of {engineering.output_path}. "
                    f"Use case: {engineering.style_directive}. "
                    f"Colors: {colors}. "
                    f"Design rationale: {engineering.design_rationale} "
                    f"Generation prompt: {engineering.main_prompt[:300]}{'...' if len(engineering.main_prompt) > 300 else ''}. "
                    f"Model: {gen_result.model_used}."
                ),
                change_summary=(
                    f"{variant.width}×{variant.height}px resize of {engineering.output_path}"
                ),
                agent=self.name,
                operation="create",
            )
            logger.info(
                "image_generation_agent_variant_written task_id=%s path=%s size=%dx%d",
                request.task_id,
                variant.output_path,
                variant.width,
                variant.height,
            )

        # ── Step 5: Structured observation ────────────────────────────────
        variant_paths = [v.output_path for v in engineering.resize_variants]
        state.evidence.add_observation(
            evidence_type=OBSERVATION_TYPE_IMAGE_GENERATED,
            producer=self.name,
            summary=(
                f"Generated {engineering.style_directive} image at "
                f"{final_w}×{final_h}px → {engineering.output_path}"
            ),
            path=engineering.output_path,
            payload={
                "prompt_engineering": {
                    "main_prompt": engineering.main_prompt,
                    "negative_prompt": engineering.negative_prompt,
                    "style_directive": engineering.style_directive,
                    "design_rationale": engineering.design_rationale,
                    "intended_colors": engineering.intended_colors,
                },
                "generation": {
                    "model": gen_result.model_used,
                    "seed": gen_result.seed_used,
                    "generation_width": gen_result.actual_width,
                    "generation_height": gen_result.actual_height,
                    "duration_ms": gen_result.generation_duration_ms,
                },
                "output": {
                    "path": engineering.output_path,
                    "final_width": final_w,
                    "final_height": final_h,
                    "format": engineering.output_format,
                    "was_resized": engineering.needs_resize,
                    "resize_mode": engineering.resize_mode if engineering.needs_resize else None,
                    "variants": variant_paths,
                    "source_original_path": (
                        _derive_source_path(engineering.output_path)
                        if engineering.needs_resize
                        else None
                    ),
                },
            },
        )

        state.add_note(
            f"Image generation complete: {engineering.output_path} "
            f"({final_w}×{final_h}px, {engineering.output_format.upper()})"
            + (
                f" + {len(engineering.resize_variants)} variant(s)"
                if engineering.resize_variants
                else ""
            )
        )

        logger.info(
            "image_generation_agent_completed task_id=%s step_id=%s path=%s variants=%d",
            request.task_id,
            step.id,
            engineering.output_path,
            len(engineering.resize_variants),
        )

        return state
