"""Impact Assessment Agent.

Classifies the scope of a user clarification after a manual_review episode and
produces a structured action plan: narrow (retry), moderate (revise tasks in place),
or disruptive (full evolutionary restart).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

# ── Input contracts ───────────────────────────────────────────────────────────


@dataclass
class BlockedTaskInfo:
    task_id: int
    title: str
    description: str | None
    validation_notes: str | None


@dataclass
class CompletedTaskSummary:
    task_id: int
    title: str


@dataclass
class PendingTaskSummary:
    task_id: int
    sequence_order: int | None
    title: str
    description: str | None


@dataclass
class ImpactAssessmentInput:
    project_goal: str
    user_clarification: str
    blocked_task: BlockedTaskInfo
    completed_tasks: list[CompletedTaskSummary] = field(default_factory=list)
    pending_tasks: list[PendingTaskSummary] = field(default_factory=list)


# ── LLM output schema ─────────────────────────────────────────────────────────


class TaskRevisionSpec(BaseModel):
    """Describes an in-place modification of an existing pending atomic task."""

    task_id: int
    new_description: str | None
    new_objective: str | None
    new_implementation_steps: str | None
    new_acceptance_criteria: str | None
    new_technical_constraints: str | None
    reason: str


class NewTaskSpec(BaseModel):
    """Describes a new atomic task to insert after an existing one."""

    insert_after_task_id: int | None
    title: str
    description: str
    objective: str
    proposed_solution: str
    implementation_steps: str
    acceptance_criteria: str
    technical_constraints: str
    reason: str


class EnvironmentDependency(BaseModel):
    """A new package or tool required by the user's clarification."""

    package_name: str
    version_constraint: str | None
    reason: str
    version_strictness: str = "any_compatible"
    # Allowed values: "exact_only" | "preferred" | "any_compatible"
    # exact_only    — user explicitly requires this exact version; escalate if unavailable
    # preferred     — install this version if possible, fall back to compatible one with a note
    # any_compatible — any version that satisfies the dependency is acceptable (default)


class ImpactAssessmentLLMOutput(BaseModel):
    change_scope: Literal["narrow", "moderate", "disruptive"]
    reasoning: str
    tasks_to_modify: list[TaskRevisionSpec] = Field(default_factory=list)
    tasks_to_add: list[NewTaskSpec] = Field(default_factory=list)
    resequence_needed: bool
    environment_changes: list[EnvironmentDependency] = Field(default_factory=list)


# ── Result ────────────────────────────────────────────────────────────────────


@dataclass
class ImpactAssessmentResult:
    change_scope: Literal["narrow", "moderate", "disruptive"]
    reasoning: str
    tasks_to_modify: list[TaskRevisionSpec]
    tasks_to_add: list[NewTaskSpec]
    resequence_needed: bool
    environment_changes: list[EnvironmentDependency] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.environment_changes is None:
            self.environment_changes = []


class ImpactAssessmentError(Exception):
    pass


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = prompt_loader.get("impact_assessment_agent")


def _render_completed(tasks: list[CompletedTaskSummary]) -> str:
    if not tasks:
        return "  (none)"
    return "\n".join(f"  - [{t.task_id}] {t.title}" for t in tasks)


def _render_pending(tasks: list[PendingTaskSummary]) -> str:
    if not tasks:
        return "  (none)"
    lines = []
    for t in tasks:
        order = t.sequence_order if t.sequence_order is not None else "?"
        desc = (t.description or "").strip()[:200]
        lines.append(f"  - [{t.task_id}] (order={order}) {t.title}")
        if desc:
            lines.append(f"    description: {desc}")
    return "\n".join(lines)


def _build_user_prompt(inp: ImpactAssessmentInput) -> str:
    from app.services.prompt_loader import prompt_loader

    prompt_loader.validate_builder_inputs(
        "impact_assessment_agent",
        "main",
        {
            "project_goal": inp.project_goal,
            "user_clarification": inp.user_clarification,
            "blocked_task": inp.blocked_task,
            "completed_tasks": inp.completed_tasks,
            "pending_tasks": inp.pending_tasks,
        },
    )
    blocked = inp.blocked_task
    return f"""
Assess the impact of the following user clarification on the project.

PROJECT GOAL:
{inp.project_goal}

USER CLARIFICATION:
{inp.user_clarification}

BLOCKED TASK (manual_review triggered here):
- task_id: {blocked.task_id}
- title: {blocked.title}
- description: {blocked.description or '(not provided)'}
- validation_notes: {blocked.validation_notes or '(not provided)'}

COMPLETED TASKS (do not modify — they are in source):
{_render_completed(inp.completed_tasks)}

PENDING TASKS (ordered by execution sequence):
{_render_pending(inp.pending_tasks)}

Instructions:
1. Determine whether the clarification requires a narrow, moderate, or disruptive change.
2. For moderate: identify exactly which pending tasks need revision and/or which new tasks
   to insert, using their task_ids. Only include tasks actually affected by the clarification.
3. For narrow or disruptive: leave tasks_to_modify and tasks_to_add empty.
4. Provide clear reasoning for your scope decision.
""".strip()


# ── Public API ────────────────────────────────────────────────────────────────


def assess_impact(inp: ImpactAssessmentInput) -> ImpactAssessmentResult:
    """
    Call the LLM to classify the scope of a user clarification and produce
    an action plan for the resumption service.
    """
    provider = get_llm_provider()
    user_prompt = _build_user_prompt(inp)

    raw = provider.generate_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="impact_assessment_output",
        json_schema=ImpactAssessmentLLMOutput.model_json_schema(),
    )

    try:
        llm_output = ImpactAssessmentLLMOutput.model_validate(raw)
    except ValidationError as exc:
        raise ImpactAssessmentError(
            f"ImpactAssessmentAgent returned invalid structured output: {exc}"
        ) from exc

    logger.info(
        "impact_assessment_complete scope=%s resequence=%s " "tasks_to_modify=%d tasks_to_add=%d",
        llm_output.change_scope,
        llm_output.resequence_needed,
        len(llm_output.tasks_to_modify),
        len(llm_output.tasks_to_add),
    )

    return ImpactAssessmentResult(
        change_scope=llm_output.change_scope,
        reasoning=llm_output.reasoning,
        tasks_to_modify=llm_output.tasks_to_modify,
        tasks_to_add=llm_output.tasks_to_add,
        resequence_needed=llm_output.resequence_needed,
        environment_changes=llm_output.environment_changes,
    )
