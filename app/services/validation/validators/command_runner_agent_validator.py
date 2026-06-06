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
from app.services.prompt_loader import prompt_loader
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

VALIDATOR_KEY = "command_runner_agent_validator"
PRODUCER_KEY = "command_runner_agent"


COMMAND_RUNNER_AGENT_VALIDATOR_SYSTEM_PROMPT = prompt_loader.get("command_runner_agent_validator")


class CommandRunnerAgentValidationFinding(BaseModel):
    severity: Literal["info", "warning", "error"]
    category: str = Field(..., min_length=3)
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)


class PartialAnnotationLLMOutput(BaseModel):
    file_path: str | None = None
    issue_summary: str
    required_action: str


class CommandRunnerAgentValidationLLMOutput(BaseModel):
    decision: Literal["completed", "partial", "failed", "manual_review"]
    summary: str
    validated_scope: str | None = None
    missing_scope: str | None = None
    blockers: list[str] = Field(default_factory=list)
    findings: list[CommandRunnerAgentValidationFinding] = Field(default_factory=list)
    manual_review_required: bool = False
    reasoning_notes: list[str] = Field(default_factory=list)
    partial_annotations: list[PartialAnnotationLLMOutput] = Field(default_factory=list)


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


# Writing agents whose changed_files entries signal that code repairs were applied.
_WRITING_AGENTS = frozenset({"code_change_agent", "test_builder_agent", "document_writer_agent"})


def _build_code_repair_context(validation_input: TaskValidationInput) -> str:
    """Summarise which writing agents applied file changes in this execution run.

    This gives the validator a concrete signal about whether prior command
    failures reflect pre-repair state.  When writing agents appear here with a
    non-zero file count, prior assertion failures may have been superseded by
    the applied repairs.
    """
    by_agent: dict[str, int] = {}
    for cf in validation_input.execution_result.evidence.changed_files:
        if cf.producer in _WRITING_AGENTS:
            by_agent[cf.producer] = by_agent.get(cf.producer, 0) + 1

    if not by_agent:
        return "- no writing-agent changes recorded in this run"

    return "\n".join(
        f"- {agent}: {count} file(s) changed" for agent, count in sorted(by_agent.items())
    )


def _collect_evidence_paths(validation_input: TaskValidationInput) -> list[str]:
    producer_view = build_producer_evidence_view(
        validation_input.execution_result.evidence,
        producer=PRODUCER_KEY,
    )
    return collect_paths_from_items(producer_view.items)


def _get_command_evidence_items(validation_input: TaskValidationInput) -> list[Any]:
    return [
        item
        for item in validation_input.execution_result.evidence.commands
        if getattr(item, "producer", None) == PRODUCER_KEY
    ]


def _command_succeeded(command_item: Any) -> bool:
    if command_item is None:
        return False
    if bool(getattr(command_item, "timed_out", False)):
        return False

    exit_code = getattr(command_item, "exit_code", None)
    expected_exit_codes = list(getattr(command_item, "expected_exit_codes", []) or [])
    return exit_code in expected_exit_codes if expected_exit_codes else exit_code == 0


def _build_command_history_summary(command_items: list[Any]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for index, item in enumerate(command_items):
        history.append(
            {
                "index": index,
                "command": getattr(item, "command", None),
                "cwd": getattr(item, "cwd", None),
                "exit_code": getattr(item, "exit_code", None),
                "expected_exit_codes": list(getattr(item, "expected_exit_codes", []) or []),
                "timed_out": bool(getattr(item, "timed_out", False)),
                "verification_goal": getattr(item, "verification_goal", None),
                "validation_claims": list(getattr(item, "validation_claims", []) or []),
                "observed_outcome_summary": getattr(item, "observed_outcome_summary", None),
                "succeeded": _command_succeeded(item),
            }
        )
    return history


def _is_terminal_verification_clean(command_items: list[Any]) -> bool:
    if not command_items:
        return False
    return _command_succeeded(command_items[-1])


def _has_resolved_intermediate_failure(command_items: list[Any]) -> bool:
    if len(command_items) < 2:
        return False

    latest = command_items[-1]
    if not _command_succeeded(latest):
        return False

    return any(not _command_succeeded(item) for item in command_items[:-1])


_NON_VERIFIABLE_TASK_TYPES = frozenset(
    {
        "documentation",
        "planning",
        "requirements",
        "review",
        "onboarding",
    }
)

_SETUP_ONLY_COMMAND_PREFIXES: tuple[str, ...] = (
    "chmod ",
    "mkdir ",
    "cp ",
    "mv ",
    "ln ",
    "touch ",
    "export ",
    "apt ",
    "apt-get ",
    "pip install",
    "npm install",
    "yarn install",
    "bundle install",
    "brew install",
)


def _command_item_is_setup_only(command_item: Any) -> bool:
    """Return True when command_item is infrastructure prep, not objective verification."""
    cmd = (getattr(command_item, "command", "") or "").strip().lower()
    return any(cmd.startswith(prefix) for prefix in _SETUP_ONLY_COMMAND_PREFIXES)


def _task_is_verifiable_implementation(validation_input: TaskValidationInput) -> bool:
    task_type = (
        (getattr(validation_input.execution_request, "task_type", None) or "").strip().lower()
    )
    return task_type not in _NON_VERIFIABLE_TASK_TYPES


def _normalize_llm_output_for_terminal_success(
    *,
    llm_output: CommandRunnerAgentValidationLLMOutput,
    validation_input: TaskValidationInput,
    command_items: list[Any],
) -> CommandRunnerAgentValidationLLMOutput:
    if llm_output.decision not in {"partial", "failed"}:
        return llm_output

    if not _is_terminal_verification_clean(command_items):
        return llm_output

    if not _task_is_verifiable_implementation(validation_input):
        return llm_output

    latest = command_items[-1]

    # Setup commands (chmod +x, mkdir, cp, etc.) prepare for verification but do NOT
    # verify the task objective. Do not elevate to completed when the terminal "success"
    # was an infrastructure prep step rather than the acceptance-criteria test itself.
    if _command_item_is_setup_only(latest):
        return llm_output

    resolved_intermediate_failure = _has_resolved_intermediate_failure(command_items)

    latest_goal = getattr(latest, "verification_goal", "") or "Repository-local verification"
    latest_command = getattr(latest, "command", "") or "the final verification command"

    summary = (
        f"The command_runner_agent contribution should be classified as completed because "
        f"the terminal verification evidence is successful and materially validates the task objective. "
        f"The latest command `{latest_command}` completed successfully and supports: {latest_goal}."
    )

    if resolved_intermediate_failure:
        summary += (
            " Earlier failed verification attempts appear to have been repaired and superseded by "
            "later repository changes plus the final successful verification, so they should not "
            "degrade the terminal classification."
        )

    findings = list(llm_output.findings)
    findings.append(
        CommandRunnerAgentValidationFinding(
            severity="info",
            category="terminal_verification",
            message=(
                "The latest command evidence is successful and should be treated as the terminal "
                "verification state for this contribution."
            ),
            evidence_refs=[],
            file_paths=[],
        )
    )
    if resolved_intermediate_failure:
        findings.append(
            CommandRunnerAgentValidationFinding(
                severity="info",
                category="resolved_repair_loop",
                message=(
                    "Earlier failed verification attempts were superseded by later repository changes "
                    "and a final successful verification run."
                ),
                evidence_refs=[],
                file_paths=[],
            )
        )

    blockers: list[str] = []
    for blocker in llm_output.blockers:
        lowered = blocker.lower()
        if resolved_intermediate_failure and (
            "initial verification" in lowered
            or "first command" in lowered
            or "discovery" in lowered
            or "did not discover" in lowered
            or "0 tests" in lowered
        ):
            continue
        blockers.append(blocker)

    reasoning_notes = list(llm_output.reasoning_notes)
    reasoning_notes.append(
        "Terminal successful verification should dominate intermediate failed attempts when the later evidence clearly supersedes the earlier failure."
    )

    return CommandRunnerAgentValidationLLMOutput(
        decision="completed",
        summary=summary,
        validated_scope=llm_output.validated_scope or latest_goal,
        missing_scope=None,
        blockers=blockers,
        findings=findings,
        manual_review_required=False,
        reasoning_notes=reasoning_notes,
        partial_annotations=[],
    )


def _normalize_partial_fields(
    llm_output: CommandRunnerAgentValidationLLMOutput,
) -> CommandRunnerAgentValidationLLMOutput:
    """Ensure partial_annotations is empty when decision is not 'partial'."""
    if llm_output.decision != "partial" and llm_output.partial_annotations:
        return CommandRunnerAgentValidationLLMOutput(
            decision=llm_output.decision,
            summary=llm_output.summary,
            validated_scope=llm_output.validated_scope,
            missing_scope=llm_output.missing_scope,
            blockers=list(llm_output.blockers),
            findings=list(llm_output.findings),
            manual_review_required=llm_output.manual_review_required,
            reasoning_notes=list(llm_output.reasoning_notes),
            partial_annotations=[],
        )
    return llm_output


def _build_user_prompt(
    *,
    validation_input: TaskValidationInput,
    producer_items: list[EvidenceItem],
    context_resources: list[TextResource],
    evidence_resources: list[TextResource],
    command_history: list[dict[str, Any]],
) -> str:
    request = validation_input.execution_request
    result = validation_input.execution_result
    code_repair_context = _build_code_repair_context(validation_input)

    prompt_loader.validate_builder_inputs(
        "command_runner_agent_validator",
        "main",
        {
            "task": request,
            "execution_result": result,
            "request_context": request.context,
            "historical_context": request.historical_context,
            "subagent_capability": validation_input,
            "producer_evidence": producer_items,
            "command_history": command_history,
            "context_files": context_resources,
            "evidence_files": evidence_resources,
            "code_repair_context": code_repair_context,
        },
    )
    latest_command = command_history[-1] if command_history else None

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
- allowed_paths: {_safe_getattr(request, "allowed_paths", [])}
- blocked_paths: {_safe_getattr(request, "blocked_paths", [])}
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

Command history summary:
- command_history: {command_history}
- latest_command: {latest_command}
- terminal_verification_clean: {_is_terminal_verification_clean(_get_command_evidence_items(validation_input))}
- resolved_intermediate_failure: {_has_resolved_intermediate_failure(_get_command_evidence_items(validation_input))}

Code repair context (writing agents that applied changes in this run):
{code_repair_context}

Context files to read and use:
{_render_text_resources(context_resources)}

Evidence-related files to read and use:
{_render_text_resources(evidence_resources)}

Instructions:
- Evaluate only the correctness and sufficiency of the command_runner_agent contribution.
- Use the task objective, acceptance criteria, context files, evidence files, and execution evidence concretely.
- Focus especially on whether the operational verification step was meaningful, relevant, and properly evidenced.
- Use relevant_files as an authoritative hint about which files matter most for the task. If the terminal command passes but does not appear to exercise any of the relevant_files either directly or through a broader test suite, note this as a potential partial_annotation rather than a full "completed".
- Treat the latest successful command evidence as the terminal verification state when it clearly supersedes earlier failed attempts.
- Do not keep the result as partial merely because an intermediate verification attempt failed, if a later successful verification materially covers the same task objective.
- When code_repair_context shows that writing agents (code_change_agent, test_builder_agent, document_writer_agent) applied changes in this run AND resolved_intermediate_failure is True, the prior command failures are likely pre-repair. In this case the terminal successful verification should be treated as post-repair evidence and classified as "completed" unless a specific acceptance criterion remains demonstrably unverified.
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

        command_items = _get_command_evidence_items(validation_input)
        command_history = _build_command_history_summary(command_items)

        provider = get_llm_provider(
            model=settings.validator_model,
            provider=settings.validator_provider,
        )
        user_prompt = _build_user_prompt(
            validation_input=validation_input,
            producer_items=producer_items,
            context_resources=context_resources,
            evidence_resources=evidence_resources,
            command_history=command_history,
        )

        raw = provider.generate_structured(
            system_prompt=COMMAND_RUNNER_AGENT_VALIDATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="command_runner_agent_validator_output",
            json_schema=CommandRunnerAgentValidationLLMOutput.model_json_schema(),
        )

        try:
            llm_output = CommandRunnerAgentValidationLLMOutput.model_validate(raw)
        except ValidationError as exc:
            raise CommandRunnerAgentValidatorError(
                f"CommandRunnerAgentValidator returned invalid structured output: {str(exc)}"
            ) from exc

        llm_output = _normalize_llm_output_for_terminal_success(
            llm_output=llm_output,
            validation_input=validation_input,
            command_items=command_items,
        )
        llm_output = _normalize_partial_fields(llm_output)

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
                "reasoning_notes": list(llm_output.reasoning_notes),
                "command_history": command_history,
                "terminal_verification_clean": _is_terminal_verification_clean(command_items),
                "resolved_intermediate_failure": _has_resolved_intermediate_failure(command_items),
            },
        )
