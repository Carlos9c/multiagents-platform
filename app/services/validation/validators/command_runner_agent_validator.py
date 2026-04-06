from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.execution_engine.capabilities import get_subagent_capability
from app.execution_engine.contracts import EvidenceItem
from app.models.task import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema
from app.services.validation.base import BaseTaskValidator
from app.services.validation.contracts import (
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

VALIDATOR_KEY = "command_runner_agent_validator"
PRODUCER_KEY = "command_runner_agent"


COMMAND_RUNNER_AGENT_VALIDATOR_SYSTEM_PROMPT = """
You are the validator for the command_runner_agent contribution.

Your job is to evaluate whether the command_runner_agent contribution is correct with respect to the task objective and the provided execution evidence.

You MUST evaluate using:
- the task definition
- the execution result
- the relevant context files
- the evidence files related to command_runner_agent
- the evidence items produced by command_runner_agent

You are NOT the executor.
You are NOT a planner.
You are NOT a reviewer proposing improvements.
You do NOT suggest ideas, enhancements, refactors, or future work.

You must only evaluate:
- whether the operational verification contribution was appropriate for the task
- whether the chosen verification step was meaningful and relevant
- whether the command evidence supports, weakens, or contradicts the claimed completed scope
- whether the verification evidence is sufficient, partial, failed, or ambiguous
- whether there are blockers or contradictions
- whether the result should be classified as completed, partial, failed, or manual_review

Critical rules:
- Do not propose improvements.
- Do not suggest alternative commands.
- Do not evaluate what should be done next.
- Only judge the task objective and the actual evidence provided.
- Use the provided file contents and command evidence concretely.
- Ground every conclusion in the provided task data, evidence items, command outputs, and file contents.
- If the evidence is insufficient to evaluate reliably, choose manual_review.
- If the verification is meaningful but incomplete or weak, choose partial.
- If the verification clearly contradicts the task objective or claimed success, choose failed.

Return ONLY JSON matching the provided schema.
""".strip()


class CommandRunnerAgentValidationFinding(BaseModel):
    severity: Literal["info", "warning", "error"]
    category: str = Field(..., min_length=3)
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)


class CommandRunnerAgentValidationLLMOutput(BaseModel):
    decision: Literal["completed", "partial", "failed", "manual_review"]
    summary: str
    validated_scope: str | None = None
    missing_scope: str | None = None
    blockers: list[str] = Field(default_factory=list)
    findings: list[CommandRunnerAgentValidationFinding] = Field(default_factory=list)
    manual_review_required: bool = False
    reasoning_notes: list[str] = Field(default_factory=list)


class CommandRunnerAgentValidatorError(Exception):
    """Raised when command_runner_agent validation cannot be completed."""


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


def _build_user_prompt(
    *,
    validation_input: TaskValidationInput,
    producer_items: list[EvidenceItem],
    context_resources: list[TextResource],
    evidence_resources: list[TextResource],
) -> str:
    request = validation_input.execution_request
    result = validation_input.execution_result

    return f"""
Validate the command_runner_agent contribution for this task.

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

Execution result:
- decision: {result.decision}
- summary: {result.summary}
- details: {result.details}
- rejection_reason: {result.rejection_reason}
- completed_scope: {result.completed_scope}
- remaining_scope: {result.remaining_scope}
- blockers_found: {result.blockers_found}
- validation_notes: {result.validation_notes}
- output_snapshot: {result.output_snapshot}
- execution_agent_sequence: {result.execution_agent_sequence}

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

Instructions:
- Evaluate only the correctness and sufficiency of the command_runner_agent contribution.
- Use the task objective, acceptance criteria, context files, evidence files, and execution evidence concretely.
- Focus especially on whether the operational verification step was meaningful, relevant, and properly evidenced.
- Do not suggest improvements.
- Do not propose future work.
- Do not act as a reviewer proposing better commands.
- Decide only whether the contribution should be classified as completed, partial, failed, or manual_review.
- Explain the decision through evidence-based findings and blockers when applicable.
""".strip()


class CommandRunnerAgentValidator(BaseTaskValidator):
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

        provider = get_llm_provider()
        strict_schema = to_openai_strict_json_schema(
            CommandRunnerAgentValidationLLMOutput.model_json_schema()
        )

        user_prompt = _build_user_prompt(
            validation_input=validation_input,
            producer_items=producer_items,
            context_resources=context_resources,
            evidence_resources=evidence_resources,
        )

        raw = provider.generate_structured(
            system_prompt=COMMAND_RUNNER_AGENT_VALIDATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="command_runner_agent_validator_output",
            json_schema=strict_schema,
        )

        try:
            llm_output = CommandRunnerAgentValidationLLMOutput.model_validate(raw)
        except ValidationError as exc:
            raise CommandRunnerAgentValidatorError(
                f"CommandRunnerAgentValidator returned invalid structured output: {str(exc)}"
            ) from exc

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
            metadata={
                "producer_key": self.producer_key,
                "producer_evidence_count": len(producer_items),
                "context_file_count": len(context_resources),
                "evidence_file_count": len(evidence_resources),
                "reasoning_notes": list(llm_output.reasoning_notes),
            },
        )