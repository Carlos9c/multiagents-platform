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

VALIDATOR_KEY = "code_change_agent_validator"
PRODUCER_KEY = "code_change_agent"

FORBIDDEN_FINDING_CATEGORIES = {
    "execution_evidence",
    "scope_consistency",
    "pipeline_consistency",
    "promotion_state",
    "validator_conflict",
    "aggregator_conflict",
    "workflow_state",
    "orchestrator_state",
    "execution_result_state",
}

FORBIDDEN_TEXT_SNIPPETS = (
    "execution result",
    "result marked as `partial`",
    "result marked as partial",
    "only completed an operational pass",
    "operational pass",
    "promotion",
    "promoted",
    "workspace_promoted",
    "other validator",
    "another validator",
    "command_runner_agent",
    "context_selection_agent",
    "execution evidence does not confirm",
    "no evidence of execution",
    "no test execution evidence",
    "pipeline",
    "aggregator",
    "workflow",
    "orchestrator",
)

CODE_CHANGE_AGENT_VALIDATOR_SYSTEM_PROMPT = """
You are the validator for the code_change_agent contribution.

Your job is to evaluate only the correctness and task-level sufficiency of the
code/materialized file contribution produced by code_change_agent.

You MUST evaluate using:
- the task definition
- the acceptance criteria
- the technical constraints
- the relevant context files
- the evidence files related to code_change_agent
- the evidence items produced by code_change_agent

You MAY use supplemental non-code verification evidence only as a supporting
signal about whether the code behaves as expected. This supplemental evidence
must never be used to judge:
- the execution pipeline
- the orchestrator result
- whether another agent did its job
- whether promotion should happen
- whether the overall workflow is internally consistent

You are NOT:
- the executor
- the planner
- the orchestrator
- the validation aggregator
- a reviewer proposing improvements
- an auditor of other validators or other agents

Your scope is ONLY:
- whether the changed code/files satisfy the task objective
- whether the changed code/files satisfy the acceptance criteria
- whether the changed code/files respect the stated constraints
- whether there is meaningful code/task scope still missing
- whether there are code-grounded blockers or contradictions

Strict boundary rules:
- Do not judge the overall execution result state.
- Do not lower the decision because the execution engine said partial.
- Do not lower the decision because promotion did not occur.
- Do not lower the decision because another agent may or may not have produced evidence.
- Do not emit findings about workflow, pipeline, promotion, orchestration, or validator disagreement.
- Do not propose improvements, refactors, enhancements, or future work.
- Ground every conclusion in the task definition, concrete file contents, and code_change_agent evidence.
- If supplemental verification exists, you may mention it only as support for code correctness.
- If the code evidence is insufficient to evaluate the code itself, choose manual_review.
- If the code is partially aligned but incomplete, choose partial.
- If the code clearly contradicts the task objective or acceptance criteria, choose failed.
- If the code materially satisfies the task objective and acceptance criteria, choose completed.

Generated documentation is NOT authoritative ground truth:
- Context files such as spec documents, README files, architecture docs, and design notes loaded
  from the project repository were produced by prior agents as outputs, NOT as inputs.
- These generated documents may contain imprecise, overly strict, or contradictory rules compared
  to the actual task definition and the user's original intent.
- The authoritative sources for validation are, in order of priority:
  1. The task's acceptance_criteria and description (closest to user intent).
  2. The code/file content itself and the evidence produced by code_change_agent.
  3. Generated context documents — treated as supplemental hints only.
- If a generated spec document contradicts the task acceptance_criteria, prefer the task
  acceptance_criteria. Do NOT fail or mark partial solely because the code deviates from a
  generated spec that was itself inconsistent with the task definition.

Default-to-completed rule when verification evidence is present (burden of proof):
- When supplemental non-code verification evidence shows a terminal command succeeded
  (exit_code=0) and its goal covers the core task objective, your default decision is
  "completed" for the code contribution.
- You may only override this default to "partial" if you can identify a SPECIFIC structural
  gap directly visible in the file contents: a missing function, a wrong implementation, an
  absent integration point explicitly required by the acceptance criteria.
- "The test might not cover everything" or "additional functionality could be added" are NOT
  grounds for "partial" when the terminal verification passed. Do not declare partial to be
  cautious when the evidence does not concretely support it.
- Only annotate partial_annotations for gaps you can locate in the actual file contents or
  acceptance criteria. Do not annotate gaps you infer might exist without direct evidence.

Partial decision rules (apply ONLY when decision is 'partial'):
When decision is 'partial', you MUST populate partial_annotations with one or more entries
describing each specific gap that prevents the task from being complete. Each annotation requires:
  - file_path: the relative path of the file that has a gap (exactly as it appears in the evidence),
    or null if the gap is not specific to a single file
  - issue_summary: a concrete description of what is incomplete or wrong in that file/area
  - required_action: exactly what must be done to close this gap — be specific and actionable,
    since recovery will use this to generate a precise follow-up task

Rules for partial_annotations:
- Only annotate gaps you have concrete evidence for from the task definition, acceptance criteria,
  and file contents. Do not annotate speculatively or preemptively.
- Do not use rejected_files — describe what is incomplete or wrong via partial_annotations instead.
- If decision is not 'partial', leave partial_annotations empty.
- Each required_action must be actionable: recovery consumes these annotations to create follow-up tasks.

Return ONLY JSON matching the provided schema.
""".strip()


class CodeChangeAgentValidationFinding(BaseModel):
    severity: Literal["info", "warning", "error"]
    category: str = Field(..., min_length=3)
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)


class PartialAnnotationLLMOutput(BaseModel):
    file_path: str | None = None
    issue_summary: str
    required_action: str


class CodeChangeAgentValidationLLMOutput(BaseModel):
    decision: Literal["completed", "partial", "failed", "manual_review"]
    summary: str
    validated_scope: str | None = None
    missing_scope: str | None = None
    blockers: list[str] = Field(default_factory=list)
    findings: list[CodeChangeAgentValidationFinding] = Field(default_factory=list)
    manual_review_required: bool = False
    reasoning_notes: list[str] = Field(default_factory=list)
    partial_annotations: list[PartialAnnotationLLMOutput] = Field(default_factory=list)


class CodeChangeAgentValidatorError(Exception):
    """Raised when code_change_agent validation cannot be completed."""


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


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains_forbidden_text(value: Any) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    return any(snippet in text for snippet in FORBIDDEN_TEXT_SNIPPETS)


def _is_forbidden_finding(finding: CodeChangeAgentValidationFinding) -> bool:
    if finding.category in FORBIDDEN_FINDING_CATEGORIES:
        return True
    if _contains_forbidden_text(finding.message):
        return True
    return False


def _is_forbidden_blocker(text: str) -> bool:
    return _contains_forbidden_text(text)


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

    if capability.uses_tools:
        lines.append("- uses_tools:")
        lines.extend([f"  - {item}" for item in capability.uses_tools])

    if capability.strengths:
        lines.append("- strengths:")
        lines.extend([f"  - {item}" for item in capability.strengths])

    if capability.limits:
        lines.append("- limits:")
        lines.extend([f"  - {item}" for item in capability.limits])

    if capability.usage_guidance:
        lines.append("- usage_guidance:")
        lines.extend([f"  - {item}" for item in capability.usage_guidance])

    return "\n".join(lines)


def _collect_supporting_verification_items(
    validation_input: TaskValidationInput,
) -> list[EvidenceItem]:
    """
    Collect non-code evidence that can support confidence in code correctness,
    without turning this validator into an auditor of the full workflow.
    """
    supporting: list[EvidenceItem] = []

    for item in validation_input.execution_result.evidence.to_evidence_items():
        if item.producer == PRODUCER_KEY:
            continue
        if item.evidence_type in {"command_execution", "file_read"}:
            supporting.append(item)

    return supporting


def _render_supporting_verification_items(items: list[EvidenceItem]) -> str:
    if not items:
        return "- none"

    lines: list[str] = []
    for index, item in enumerate(items):
        lines.append(f"- support_ref: support:{index}")
        lines.append(f"  evidence_type: {item.evidence_type}")
        lines.append(f"  producer: {item.producer}")
        lines.append(f"  path: {item.path}")
        lines.append(f"  summary: {item.summary}")
        lines.append(f"  payload: {item.payload}")
    return "\n".join(lines)


def _build_user_prompt(
    *,
    validation_input: TaskValidationInput,
    producer_items: list[EvidenceItem],
    context_resources: list[TextResource],
    evidence_resources: list[TextResource],
    supporting_items: list[EvidenceItem],
) -> str:
    request = validation_input.execution_request

    return f"""
Validate the code_change_agent contribution for this task.

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
- tests_required: {_safe_getattr(request, "tests_required")}
- technical_constraints: {_safe_getattr(request, "technical_constraints")}
- out_of_scope: {_safe_getattr(request, "out_of_scope")}
- success_criteria: {_safe_getattr(request, "success_criteria")}
- constraints: {_safe_getattr(request, "constraints")}
- executor_type: {_safe_getattr(request, "executor_type")}

Request context:
- allowed_paths: {_safe_getattr(request.context, "allowed_paths", [])}
- blocked_paths: {_safe_getattr(request.context, "blocked_paths", [])}
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

Context files to read and use:
{_render_text_resources(context_resources)}

Evidence-related files to read and use:
{_render_text_resources(evidence_resources)}

Supplemental non-code verification evidence (support only):
{_render_supporting_verification_items(supporting_items)}

Instructions:
- Evaluate only the correctness and task-level sufficiency of the code_change_agent contribution.
- Base the decision primarily on the changed code/files and their concrete contents.
- Use supplemental non-code verification evidence only as support for code correctness, never as a reason to judge workflow state.
- Do not judge execution_result state, promotion state, orchestrator behavior, or other validators.
- Do not emit findings or blockers about pipeline consistency, workflow consistency, or lack of evidence from other producers.
- Do not suggest improvements.
- Do not propose future work.
- Do not act as a style reviewer.
- Decide only whether the code contribution should be classified as completed, partial, failed, or manual_review.
- Explain the decision through evidence-based findings and blockers grounded in the code and task.
""".strip()


def _sanitize_llm_output(
    llm_output: CodeChangeAgentValidationLLMOutput,
) -> CodeChangeAgentValidationLLMOutput:
    sanitized_findings = [
        finding for finding in llm_output.findings if not _is_forbidden_finding(finding)
    ]
    sanitized_blockers = [
        blocker for blocker in llm_output.blockers if not _is_forbidden_blocker(blocker)
    ]
    sanitized_reasoning_notes = [
        note for note in llm_output.reasoning_notes if not _contains_forbidden_text(note)
    ]

    sanitized_summary = llm_output.summary
    sanitized_validated_scope = llm_output.validated_scope
    sanitized_missing_scope = llm_output.missing_scope

    if _contains_forbidden_text(sanitized_summary):
        sanitized_summary = (
            "The decision is based only on the code_change_agent contribution and "
            "its task-level correctness against the provided requirements."
        )

    if _contains_forbidden_text(sanitized_validated_scope):
        sanitized_validated_scope = None

    if _contains_forbidden_text(sanitized_missing_scope):
        sanitized_missing_scope = None

    negative_signal_count = 0
    for finding in sanitized_findings:
        if finding.severity in {"warning", "error"}:
            negative_signal_count += 1
    negative_signal_count += len(sanitized_blockers)
    if sanitized_missing_scope:
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

    return CodeChangeAgentValidationLLMOutput(
        decision=decision,
        summary=sanitized_summary,
        validated_scope=sanitized_validated_scope,
        missing_scope=sanitized_missing_scope,
        blockers=sanitized_blockers,
        findings=sanitized_findings,
        manual_review_required=manual_review_required,
        reasoning_notes=sanitized_reasoning_notes,
        partial_annotations=sanitized_partial_annotations,
    )


class CodeChangeAgentValidator(BaseTaskValidator):
    validator_key = VALIDATOR_KEY
    producer_key = PRODUCER_KEY

    def validate(self, validation_input: TaskValidationInput) -> ValidationResult:
        producer_view = build_producer_evidence_view(
            validation_input.execution_result.evidence,
            producer=self.producer_key,
        )
        producer_items = producer_view.items

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
        supporting_items = _collect_supporting_verification_items(validation_input)

        provider = get_llm_provider(
            model=settings.validator_model,
            provider=settings.validator_provider,
        )
        user_prompt = _build_user_prompt(
            validation_input=validation_input,
            producer_items=producer_items,
            context_resources=context_resources,
            evidence_resources=evidence_resources,
            supporting_items=supporting_items,
        )

        raw = provider.generate_structured(
            system_prompt=CODE_CHANGE_AGENT_VALIDATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="code_change_agent_validator_output",
            json_schema=CodeChangeAgentValidationLLMOutput.model_json_schema(),
        )

        try:
            llm_output = CodeChangeAgentValidationLLMOutput.model_validate(raw)
        except ValidationError as exc:
            raise CodeChangeAgentValidatorError(
                f"CodeChangeAgentValidator returned invalid structured output: {str(exc)}"
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
                else "code"
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
                "supporting_verification_item_count": len(supporting_items),
                "reasoning_notes": list(llm_output.reasoning_notes),
                "boundary_enforcement": {
                    "forbidden_finding_categories": sorted(FORBIDDEN_FINDING_CATEGORIES),
                    "forbidden_text_snippet_count": len(FORBIDDEN_TEXT_SNIPPETS),
                },
            },
        )
