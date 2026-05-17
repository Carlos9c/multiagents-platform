from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.execution_engine.capabilities import get_subagent_capability
from app.execution_engine.contracts import EvidenceItem
from app.models.task import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
)
from app.services.llm.factory import get_llm_provider
from app.services.validation.base import BaseTaskValidator
from app.services.validation.contracts import (
    PartialAnnotation,
    TaskValidationInput,
    ValidationFinding,
    ValidationResult,
)
from app.services.validation.helpers.evidence import (
    build_producer_evidence_view,
    collect_paths_from_items,
)
from app.services.validation.helpers.resources import (
    TextResource,
    read_many_text_resources,
)

VALIDATOR_KEY = "document_writer_agent_validator"
PRODUCER_KEY = "document_writer_agent"

DOCUMENT_WRITER_AGENT_VALIDATOR_SYSTEM_PROMPT = """
You are the validator for the document_writer_agent contribution.

Your job is to evaluate only the completeness and task-level sufficiency of the
documentation and design artifacts produced by document_writer_agent.

You MUST evaluate using:
- the task definition
- the acceptance criteria
- the technical constraints
- the relevant context files
- the evidence items produced by document_writer_agent
- the actual file contents produced

You are NOT:
- the executor
- the planner
- the orchestrator
- the validation aggregator
- a code reviewer
- a style reviewer proposing improvements

Your scope is ONLY:
- whether the produced artifacts satisfy the task objective
- whether the produced artifacts satisfy the acceptance criteria
- whether the content is substantive (not placeholder or skeleton)
- whether there is meaningful scope still missing from the artifacts
- whether there are content-grounded blockers (e.g., required sections absent, wrong format)

Strict boundary rules:
- Do not judge code quality, syntax, or compilation.
- Do not judge execution pipeline state, promotion state, or orchestrator behavior.
- Do not propose improvements, refactors, or future work beyond what the task requires.
- Do not lower the decision because of missing command execution evidence — documentation
  tasks do not require command execution evidence.
- Ground every conclusion in the task definition, acceptance criteria, and actual file contents.
- If file content is insufficient to evaluate completeness, choose manual_review.
- If the artifacts are partially aligned but missing substantive required sections, choose partial.
- If the artifacts clearly contradict the task objective or acceptance criteria, choose failed.
- If the artifacts materially satisfy the task objective and acceptance criteria, choose completed.

Decision guidance:
- completed: the artifacts exist, contain substantive content, and satisfy the acceptance criteria.
- partial: the artifacts exist but one or more required sections or files are missing or
  are placeholders/skeletons with no real content.
- failed: no usable artifacts were produced, or the content fundamentally contradicts the task.
- manual_review: the evidence is too ambiguous to make a confident automated judgment.

Partial decision rules (apply ONLY when decision is 'partial'):
When decision is 'partial', you MUST populate partial_annotations with one or more entries
describing each specific gap. Each annotation requires:
  - file_path: the relative path of the file that has a gap, or null if cross-cutting
  - issue_summary: a concrete description of what is incomplete or wrong
  - required_action: exactly what must be done to close this gap (actionable, recovery will use this)

Rules for partial_annotations:
- Only annotate gaps with concrete evidence from the task definition and file contents.
- Do not annotate speculatively.
- If decision is not 'partial', leave partial_annotations empty.
- Each required_action must be specific and actionable.

Return ONLY JSON matching the provided schema.
""".strip()


class DocumentWriterValidationFinding(BaseModel):
    severity: Literal["info", "warning", "error"]
    category: str = Field(..., min_length=3)
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)


class PartialAnnotationLLMOutput(BaseModel):
    file_path: str | None = None
    issue_summary: str
    required_action: str


class DocumentWriterValidationLLMOutput(BaseModel):
    decision: Literal["completed", "partial", "failed", "manual_review"]
    summary: str
    validated_scope: str | None = None
    missing_scope: str | None = None
    blockers: list[str] = Field(default_factory=list)
    findings: list[DocumentWriterValidationFinding] = Field(default_factory=list)
    manual_review_required: bool = False
    reasoning_notes: list[str] = Field(default_factory=list)
    partial_annotations: list[PartialAnnotationLLMOutput] = Field(default_factory=list)


class DocumentWriterAgentValidatorError(Exception):
    """Raised when document_writer_agent validation cannot be completed."""


def _map_decision_to_final_task_status(decision: str) -> str | None:
    if decision == "completed":
        return TASK_STATUS_COMPLETED
    if decision == "partial":
        return TASK_STATUS_PARTIAL
    if decision in {"failed", "manual_review"}:
        return TASK_STATUS_FAILED
    return None


def _build_evidence_ref(item: EvidenceItem, index: int) -> str:
    return f"{item.evidence_type}:{index}"


def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def _model_dump_list(items: list[Any] | None) -> list[dict[str, Any]]:
    dumped: list[dict[str, Any]] = []
    for item in items or []:
        if hasattr(item, "model_dump"):
            dumped.append(item.model_dump())
        else:
            dumped.append({"value": str(item)})
    return dumped


def _render_evidence_items(items: list[EvidenceItem]) -> str:
    if not items:
        return "- none"
    lines: list[str] = []
    for index, item in enumerate(items):
        lines.append(f"- evidence_ref: {_build_evidence_ref(item, index)}")
        lines.append(f"  evidence_type: {item.evidence_type}")
        lines.append(f"  producer: {item.producer}")
        lines.append(f"  path: {item.path}")
        lines.append(f"  summary: {item.summary}")
        lines.append(f"  payload: {item.payload}")
    return "\n".join(lines)


def _render_text_resources(resources: list[TextResource]) -> str:
    if not resources:
        return "- none"
    blocks: list[str] = []
    for resource in resources:
        blocks.append(f"- logical_path: {resource.logical_path}")
        blocks.append(f"  resolved_path: {resource.resolved_path}")
        blocks.append(f"  source_kind: {resource.source_kind}")
        blocks.append(f"  exists: {resource.exists}")
        blocks.append(f"  error: {resource.error}")
        blocks.append("  content: |")
        content = resource.content or ""
        if not content:
            blocks.append("    ")
        else:
            for line in content.splitlines():
                blocks.append(f"    {line}")
    return "\n".join(blocks)


def _render_subagent_capability(validation_input: TaskValidationInput) -> str:
    capability = get_subagent_capability(
        validation_input.execution_request.executor_type,
        PRODUCER_KEY,
    )
    if capability is None:
        return "- subagent_capability: unavailable"
    lines: list[str] = [
        f"- name: {capability.name}",
        f"- role: {capability.role}",
    ]
    if capability.strengths:
        lines.append("- strengths:")
        lines.extend([f"  - {item}" for item in capability.strengths])
    if capability.limits:
        lines.append("- limits:")
        lines.extend([f"  - {item}" for item in capability.limits])
    return "\n".join(lines)


def _collect_context_paths(validation_input: TaskValidationInput) -> list[str]:
    ordered: list[str] = []

    def _append(path: str | None) -> None:
        if not path:
            return
        if path not in ordered:
            ordered.append(path)

    for path in validation_input.execution_request.context.relevant_files:
        _append(path)

    return ordered


def _collect_evidence_paths(validation_input: TaskValidationInput) -> list[str]:
    producer_view = build_producer_evidence_view(
        validation_input.execution_result.evidence,
        producer=PRODUCER_KEY,
    )
    return collect_paths_from_items(producer_view.items)


def _build_user_prompt(
    *,
    validation_input: TaskValidationInput,
    producer_items: list[EvidenceItem],
    context_resources: list[TextResource],
    evidence_resources: list[TextResource],
) -> str:
    request = validation_input.execution_request

    return f"""
Validate the document_writer_agent contribution for this task.

Task:
- task_id: {_safe_getattr(request, "task_id")}
- project_id: {_safe_getattr(request, "project_id")}
- title: {_safe_getattr(request, "task_title")}
- description: {_safe_getattr(request, "task_description")}
- summary: {_safe_getattr(request, "task_summary")}
- objective: {_safe_getattr(request, "objective")}
- proposed_solution: {_safe_getattr(request, "proposed_solution")}
- implementation_notes: {_safe_getattr(request, "implementation_notes")}
- implementation_steps: {_safe_getattr(request, "implementation_steps")}
- acceptance_criteria: {_safe_getattr(request, "acceptance_criteria")}
- technical_constraints: {_safe_getattr(request, "technical_constraints")}
- out_of_scope: {_safe_getattr(request, "out_of_scope")}
- executor_type: {_safe_getattr(request, "executor_type")}

Request context:
- relevant_files: {request.context.relevant_files}
- key_decisions: {request.context.key_decisions}
- related_tasks: {_model_dump_list(request.context.related_tasks)}

Historical context:
- historical_context_present: {request.historical_context is not None}
- historical_task_runs: {[] if request.historical_context is None else _model_dump_list(request.historical_context.selected_task_runs)}

Subagent being validated:
{_render_subagent_capability(validation_input)}

Primary evidence to evaluate (producer = {PRODUCER_KEY}):
{_render_evidence_items(producer_items)}

Context files:
{_render_text_resources(context_resources)}

Produced artifact files to evaluate:
{_render_text_resources(evidence_resources)}

Instructions:
- Evaluate only the completeness and task-level sufficiency of the document_writer_agent contribution.
- Base the decision on the produced artifact contents and their alignment with the acceptance criteria.
- Do not judge execution pipeline state, command execution evidence, or other agents.
- Do not propose improvements or future work beyond the stated task scope.
- Decide only: completed, partial, failed, or manual_review.
- For partial: populate partial_annotations with specific, actionable gaps grounded in file contents.
""".strip()


def _sanitize_llm_output(
    llm_output: DocumentWriterValidationLLMOutput,
) -> DocumentWriterValidationLLMOutput:
    negative_signal_count = 0
    for finding in llm_output.findings:
        if finding.severity in {"warning", "error"}:
            negative_signal_count += 1
    negative_signal_count += len(llm_output.blockers)
    if llm_output.missing_scope:
        negative_signal_count += 1
    if llm_output.partial_annotations:
        negative_signal_count += 1

    decision = llm_output.decision
    manual_review_required = llm_output.manual_review_required

    if decision in {"partial", "failed", "manual_review"} and negative_signal_count == 0:
        decision = "completed"
        manual_review_required = False

    sanitized_partial_annotations = (
        list(llm_output.partial_annotations) if decision == "partial" else []
    )

    return DocumentWriterValidationLLMOutput(
        decision=decision,
        summary=llm_output.summary,
        validated_scope=llm_output.validated_scope,
        missing_scope=llm_output.missing_scope,
        blockers=llm_output.blockers,
        findings=llm_output.findings,
        manual_review_required=manual_review_required,
        reasoning_notes=llm_output.reasoning_notes,
        partial_annotations=sanitized_partial_annotations,
    )


class DocumentWriterAgentValidator(BaseTaskValidator):
    validator_key = VALIDATOR_KEY
    producer_key = PRODUCER_KEY

    def validate(self, validation_input: TaskValidationInput) -> ValidationResult:
        producer_view = build_producer_evidence_view(
            validation_input.execution_result.evidence,
            producer=self.producer_key,
        )
        producer_items = producer_view.items

        # Structural guard: if no changed_file evidence, fail immediately without LLM call
        has_file_evidence = any(
            item.evidence_type == "changed_file" for item in producer_items
        )
        if not has_file_evidence:
            return ValidationResult(
                validator_key=self.validator_key,
                discipline=(
                    validation_input.intent.discipline
                    if validation_input.intent is not None
                    else "documentation"
                ),
                decision="failed",
                summary="document_writer_agent produced no file evidence.",
                findings=[],
                blockers=["No documentation artifacts were created or modified."],
                manual_review_required=False,
                final_task_status=TASK_STATUS_FAILED,
                artifacts_created=[],
                validated_evidence_ids=[],
                unconsumed_evidence_ids=[],
                followup_validation_required=False,
                recommended_next_validator_keys=[],
                partial_validation_summary=None,
                partial_annotations=[],
                metadata={"producer_key": self.producer_key, "structural_check": "no_file_evidence"},
            )

        context_paths = _collect_context_paths(validation_input)
        evidence_paths = _collect_evidence_paths(validation_input)

        context_resources = read_many_text_resources(
            validation_input.execution_request,
            logical_paths=context_paths,
        )
        evidence_resources = read_many_text_resources(
            validation_input.execution_request,
            logical_paths=evidence_paths,
        )

        provider = get_llm_provider(
            model=settings.validator_model,
            provider=settings.validator_provider,
        )
        user_prompt = _build_user_prompt(
            validation_input=validation_input,
            producer_items=producer_items,
            context_resources=context_resources,
            evidence_resources=evidence_resources,
        )

        raw = provider.generate_structured(
            system_prompt=DOCUMENT_WRITER_AGENT_VALIDATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="document_writer_agent_validator_output",
            json_schema=DocumentWriterValidationLLMOutput.model_json_schema(),
        )

        try:
            llm_output = DocumentWriterValidationLLMOutput.model_validate(raw)
        except ValidationError as exc:
            raise DocumentWriterAgentValidatorError(
                f"DocumentWriterAgentValidator returned invalid structured output: {str(exc)}"
            ) from exc

        llm_output = _sanitize_llm_output(llm_output)

        validated_evidence_ids = [
            _build_evidence_ref(item, index) for index, item in enumerate(producer_items)
        ]

        return ValidationResult(
            validator_key=self.validator_key,
            discipline=(
                validation_input.intent.discipline
                if validation_input.intent is not None
                else "documentation"
            ),
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
            unconsumed_evidence_ids=[],
            followup_validation_required=False,
            recommended_next_validator_keys=[],
            partial_validation_summary=None,
            partial_annotations=[
                PartialAnnotation(
                    file_path=a.file_path,
                    issue_summary=a.issue_summary,
                    required_action=a.required_action,
                )
                for a in llm_output.partial_annotations
            ],
            metadata={
                "producer_key": self.producer_key,
                "producer_evidence_count": len(producer_items),
                "context_file_count": len(context_resources),
                "evidence_file_count": len(evidence_resources),
                "reasoning_notes": list(llm_output.reasoning_notes),
            },
        )
