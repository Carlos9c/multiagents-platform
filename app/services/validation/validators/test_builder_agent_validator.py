from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.execution_engine.capabilities import get_subagent_capability
from app.execution_engine.contracts import OBSERVATION_TYPE_TEST_COVERAGE, EvidenceItem
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

VALIDATOR_KEY = "test_builder_agent_validator"
PRODUCER_KEY = "test_builder_agent"

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
    "code_change_agent",
    "command_runner_agent",
    "context_selection_agent",
    "document_writer_agent",
    "execution evidence does not confirm",
    "no evidence of execution",
    "no command execution evidence",
    "pipeline",
    "aggregator",
    "workflow",
    "orchestrator",
)

TEST_BUILDER_AGENT_VALIDATOR_SYSTEM_PROMPT = """
You are the validator for the test_builder_agent contribution.

Your job is to evaluate only the quality, correctness, and task-level sufficiency of the
test files produced by test_builder_agent.

You MUST evaluate using:
- the task definition
- the acceptance criteria (authoritative specification — primary source of truth)
- the technical constraints
- the relevant context files
- the evidence files produced by test_builder_agent
- the TestCoverageObservation (if available in evidence items)

You are NOT:
- the executor
- the planner
- the orchestrator
- the validation aggregator
- an auditor of other validators or other agents
- a reviewer of implementation code quality

Your scope is ONLY:
- whether the test files adequately cover the acceptance_criteria
- whether the tests are correctly structured and runnable
- whether the tests would actually fail when the implementation is wrong
- whether there is meaningful test scope still missing
- whether there are structural blockers in the test files themselves

Strict boundary rules:
- Do not judge the overall execution result state.
- Do not lower the decision because the execution engine said partial.
- Do not lower the decision because promotion did not occur.
- Do not lower the decision because the implementation code is incomplete.
- Do not emit findings about workflow, pipeline, promotion, orchestration, or validator disagreement.
- Do not propose improvements, refactors, enhancements, or future work.
- Ground every conclusion in the acceptance_criteria, concrete test file contents, and test_builder_agent evidence.

Decision guide:
- completed: the test files materially cover the acceptance_criteria and are correctly structured.
- partial: the test files exist and are correct but either (a) some acceptance criteria are
  not covered by any test case, or (b) the TestCoverageObservation indicates potential_implementation_gaps —
  meaning the tests are correct but they will fail because the implementation is structurally missing
  something required by the acceptance criteria. In case (b), partial_annotations must describe what
  the implementation is missing so a recovery pass can create a follow-up implementation task.
- failed: the test files are structurally broken (wrong imports, placeholder tests with `assert True` or
  `pass`, testing the wrong objective, not runnable with the project's standard test runner).
- manual_review: the evidence is insufficient to evaluate the tests against the acceptance_criteria.

Implementation gap handling (critical):
- When potential_implementation_gaps are present, the tests themselves are NOT wrong.
- Use decision=partial and populate partial_annotations to describe what the IMPLEMENTATION must provide.
- Each partial_annotation should target the gap in the IMPLEMENTATION, not in the test.
- required_action must tell a recovery agent what implementation work is needed to close the gap.
- Do NOT fail the test contribution because the implementation has a gap.

Generated documentation is NOT authoritative ground truth:
- Context files such as spec documents, README files, architecture docs, and design notes loaded
  from the project repository were produced by prior agents as outputs, NOT as inputs.
- The authoritative sources for validation are, in order of priority:
  1. The task's acceptance_criteria and description (closest to user intent).
  2. The test file contents and the evidence produced by test_builder_agent.
  3. Generated context documents — treated as supplemental hints only.

Partial decision rules (apply ONLY when decision is 'partial'):
When decision is 'partial', you MUST populate partial_annotations with one or more entries
describing each specific gap. Each annotation requires:
  - file_path: the relative path of the file that has a gap (the test file if the gap is
    missing coverage, or null if the gap is a missing implementation concern), or null when
    the gap is about the implementation rather than the test file
  - issue_summary: a concrete description of what is incomplete or wrong
  - required_action: exactly what must be done to close this gap — be specific and actionable,
    since recovery will use this to generate a precise follow-up task

Rules for partial_annotations:
- Only annotate gaps you have concrete evidence for from the acceptance_criteria and file contents.
- Do not annotate speculatively or preemptively.
- For implementation gaps: file_path should be the implementation file path that is missing the
  functionality (if known), or null if unknown.
- If decision is not 'partial', leave partial_annotations empty.
- Each required_action must be actionable: recovery consumes these annotations to create follow-up tasks.

Return ONLY JSON matching the provided schema.
""".strip()


class TestBuilderAgentValidationFinding(BaseModel):
    severity: Literal["info", "warning", "error"]
    category: str = Field(..., min_length=3)
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)


class PartialAnnotationLLMOutput(BaseModel):
    file_path: str | None = None
    issue_summary: str
    required_action: str


class TestBuilderAgentValidationLLMOutput(BaseModel):
    decision: Literal["completed", "partial", "failed", "manual_review"]
    summary: str
    validated_scope: str | None = None
    missing_scope: str | None = None
    blockers: list[str] = Field(default_factory=list)
    findings: list[TestBuilderAgentValidationFinding] = Field(default_factory=list)
    manual_review_required: bool = False
    reasoning_notes: list[str] = Field(default_factory=list)
    partial_annotations: list[PartialAnnotationLLMOutput] = Field(default_factory=list)


class TestBuilderAgentValidatorError(Exception):
    """Raised when test_builder_agent validation cannot be completed."""


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


def _is_forbidden_finding(finding: TestBuilderAgentValidationFinding) -> bool:
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


def _extract_coverage_observation(items: list[EvidenceItem]) -> dict | None:
    """
    Extract the TestCoverageObservation payload from the producer evidence items,
    if present. Returns the raw payload dict or None.
    """
    for item in items:
        if item.evidence_type == OBSERVATION_TYPE_TEST_COVERAGE and item.payload:
            return item.payload
    return None


def _render_coverage_observation(coverage: dict | None) -> str:
    if not coverage:
        return "- test_coverage_observation: not available"

    lines = ["test_coverage_observation:"]
    covered = coverage.get("covered_cases", [])
    uncovered = coverage.get("uncovered_cases", [])
    tested_against = coverage.get("tested_against", "unknown")
    gaps = coverage.get("potential_implementation_gaps", [])
    confidence = coverage.get("confidence", "unknown")

    lines.append(f"  tested_against: {tested_against}")
    lines.append(f"  confidence: {confidence}")
    lines.append(f"  covered_cases_count: {len(covered)}")
    lines.append(f"  uncovered_cases_count: {len(uncovered)}")
    lines.append(f"  potential_implementation_gaps_count: {len(gaps)}")

    if covered:
        lines.append("  covered_cases:")
        for case in covered:
            lines.append(f"    - {case}")

    if uncovered:
        lines.append("  uncovered_cases:")
        for case in uncovered:
            lines.append(f"    - {case}")

    if gaps:
        lines.append("  potential_implementation_gaps:")
        for gap in gaps:
            lines.append(f"    - {gap}")

    return "\n".join(lines)


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


def _build_user_prompt(
    *,
    validation_input: TaskValidationInput,
    producer_items: list[EvidenceItem],
    context_resources: list[TextResource],
    evidence_resources: list[TextResource],
) -> str:
    request = validation_input.execution_request
    coverage = _extract_coverage_observation(producer_items)

    return f"""
Validate the test_builder_agent contribution for this task.

Task:
- task_id: {_safe_getattr(request, "task_id")}
- project_id: {_safe_getattr(request, "project_id")}
- title: {_safe_getattr(request, "task_title")}
- description: {_safe_getattr(request, "task_description")}
- summary: {_safe_getattr(request, "task_summary")}
- objective: {_safe_getattr(request, "objective")}
- acceptance_criteria: {_safe_getattr(request, "acceptance_criteria")}
- tests_required: {_safe_getattr(request, "tests_required")}
- technical_constraints: {_safe_getattr(request, "technical_constraints")}
- out_of_scope: {_safe_getattr(request, "out_of_scope")}
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

Coverage assessment (from test_builder_agent secondary LLM call):
{_render_coverage_observation(coverage)}

Context files to read and use:
{_render_text_resources(context_resources)}

Evidence-related files (test files produced) to read and use:
{_render_text_resources(evidence_resources)}

Instructions:
- Evaluate only the correctness and task-level sufficiency of the test_builder_agent contribution.
- The acceptance_criteria is the primary authoritative specification for what the tests must cover.
- Use the TestCoverageObservation (if available) as a structured signal about coverage gaps and
  potential_implementation_gaps — it was produced by the test_builder_agent itself.
- Do NOT lower the decision because implementation code is missing or incomplete.
- If potential_implementation_gaps are present, use decision=partial and populate partial_annotations
  describing what the IMPLEMENTATION must provide — not what the tests must change.
- Do not judge execution_result state, promotion state, orchestrator behavior, or other validators.
- Do not emit findings or blockers about pipeline consistency, workflow consistency, or other agents.
- Do not suggest improvements or propose future work.
- Decide only whether the test contribution should be classified as completed, partial, failed, or manual_review.
""".strip()


def _sanitize_llm_output(
    llm_output: TestBuilderAgentValidationLLMOutput,
) -> TestBuilderAgentValidationLLMOutput:
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
            "The decision is based only on the test_builder_agent contribution and "
            "its task-level correctness against the provided acceptance criteria."
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

    return TestBuilderAgentValidationLLMOutput(
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


class TestBuilderAgentValidator(BaseTaskValidator):
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
            system_prompt=TEST_BUILDER_AGENT_VALIDATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="test_builder_agent_validator_output",
            json_schema=TestBuilderAgentValidationLLMOutput.model_json_schema(),
        )

        try:
            llm_output = TestBuilderAgentValidationLLMOutput.model_validate(raw)
        except ValidationError as exc:
            raise TestBuilderAgentValidatorError(
                f"TestBuilderAgentValidator returned invalid structured output: {str(exc)}"
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
                else "testing"
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
                "has_coverage_observation": _extract_coverage_observation(producer_items)
                is not None,
                "reasoning_notes": list(llm_output.reasoning_notes),
                "boundary_enforcement": {
                    "forbidden_finding_categories": sorted(FORBIDDEN_FINDING_CATEGORIES),
                    "forbidden_text_snippet_count": len(FORBIDDEN_TEXT_SNIPPETS),
                },
            },
        )
