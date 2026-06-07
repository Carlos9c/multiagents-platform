"""Impact Assessment Agent.

Classifies the scope of a user clarification after a manual_review episode and
produces a structured action plan: narrow (retry), moderate (revise/eliminate/add tasks),
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

# ── Shared task context ───────────────────────────────────────────────────────
# Used by both ReviewEvaluator and ImpactAssessmentAgent for cascade/dependency
# awareness. Contains the full pending task tree with hierarchy and relationships.


@dataclass
class TaskContextForReview:
    """Full task context passed to review-phase agents for cascade/dependency analysis."""

    task_id: int
    title: str
    description: str | None
    planning_level: str
    parent_task_id: int | None
    parent_title: str | None
    status: str
    depends_on_task_titles: list[str]
    acceptance_criteria: str | None
    sequence_order: int | None


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
class ImpactAssessmentInput:
    project_goal: str
    user_clarification: str
    blocked_task: BlockedTaskInfo
    # Full pending task tree — all non-terminal tasks with hierarchy and dependencies.
    # Used for cascade analysis and new_work_blocks parent_task_id resolution.
    full_task_tree: list[TaskContextForReview] = field(default_factory=list)
    completed_tasks: list[CompletedTaskSummary] = field(default_factory=list)
    language: str | None = None


# ── LLM output schema ─────────────────────────────────────────────────────────


class TaskRevisionSpec(BaseModel):
    """Describes an in-place modification of an existing pending task."""

    task_id: int
    new_description: str | None
    new_objective: str | None
    new_implementation_steps: str | None
    new_acceptance_criteria: str | None
    new_technical_constraints: str | None
    # Updated dependency list when the clarification changes what this task depends on.
    new_depends_on_task_titles: list[str] | None
    reason: str


class NewWorkBlock(BaseModel):
    """Describes a new block of work to materialise after scope change.

    The atomizer creates the actual atomic Task records from this spec.
    planning_level determines which atomizer path ResumptionService uses:
      - "high_level": creates a new top-level parent Task, then atomizes under it.
      - "atomic": calls the atomizer with inline data, anchors results under
                  the existing parent_task_id (must be an active high_level task).
    """

    title: str
    description: str
    objective: str
    proposed_solution: str
    acceptance_criteria: str
    technical_constraints: str
    out_of_scope: str
    task_type: str
    depends_on_task_titles: list[str]
    planning_level: Literal["high_level", "atomic"]
    # None when planning_level="high_level".
    # ID of an existing active high_level task when planning_level="atomic".
    parent_task_id: int | None
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
    # Direct task IDs the user wants cancelled. Cascade-up (parent cancellation when
    # ALL children cancelled) is handled mechanically by ResumptionService — not here.
    tasks_to_eliminate: list[int] = Field(default_factory=list)
    # Content changes to existing pending tasks (description, criteria, dependencies).
    tasks_to_modify: list[TaskRevisionSpec] = Field(default_factory=list)
    # New blocks of work the clarification introduces. Atomizer creates actual tasks.
    new_work_blocks: list[NewWorkBlock] = Field(default_factory=list)
    # New environment dependencies implied by the clarification (all scopes).
    environment_changes: list[EnvironmentDependency] = Field(default_factory=list)
    # Revised project description if the scope change alters the top-level goal.
    updated_project_goal: str | None = None


# ── Result ────────────────────────────────────────────────────────────────────


@dataclass
class ImpactAssessmentResult:
    change_scope: Literal["narrow", "moderate", "disruptive"]
    reasoning: str
    tasks_to_eliminate: list[int]
    tasks_to_modify: list[TaskRevisionSpec]
    new_work_blocks: list[NewWorkBlock]
    environment_changes: list[EnvironmentDependency]
    updated_project_goal: str | None = None

    def __post_init__(self) -> None:
        if self.environment_changes is None:
            self.environment_changes = []


class ImpactAssessmentError(Exception):
    pass


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = prompt_loader.get("impact_assessment_agent")


def _render_task_tree(tasks: list[TaskContextForReview]) -> str:
    if not tasks:
        return "  (none)"
    lines = []
    for t in tasks:
        parent_info = f" [parent: {t.parent_title}]" if t.parent_title else ""
        deps = ", ".join(t.depends_on_task_titles) if t.depends_on_task_titles else "none"
        order = t.sequence_order if t.sequence_order is not None else "?"
        ac = (t.acceptance_criteria or "")[:100] if t.acceptance_criteria else ""
        lines.append(
            f"  - [{t.task_id}] (order={order}, level={t.planning_level},"
            f" status={t.status}){parent_info}"
        )
        lines.append(f"    title: {t.title}")
        if t.description:
            lines.append(f"    description: {t.description[:150]}")
        if ac:
            lines.append(f"    acceptance_criteria: {ac}")
        lines.append(f"    depends_on: {deps}")
    return "\n".join(lines)


def _render_completed(tasks: list[CompletedTaskSummary]) -> str:
    if not tasks:
        return "  (none)"
    return "\n".join(f"  - [{t.task_id}] {t.title}" for t in tasks)


def _build_user_prompt(inp: ImpactAssessmentInput) -> str:
    from app.services.prompt_loader import prompt_loader

    prompt_loader.validate_builder_inputs(
        "impact_assessment_agent",
        "main",
        {
            "project_goal": inp.project_goal,
            "user_clarification": inp.user_clarification,
            "blocked_task": inp.blocked_task,
            "full_task_tree": inp.full_task_tree,
            "completed_tasks": inp.completed_tasks,
            "language": inp.language,
        },
    )
    lang_prefix = f"OUTPUT LANGUAGE: {inp.language}.\n\n" if inp.language else ""
    blocked = inp.blocked_task
    return f"""{lang_prefix}Assess the impact of the following user clarification on the project.

PROJECT GOAL:
{inp.project_goal}

USER CLARIFICATION:
{inp.user_clarification}

BLOCKED TASK (manual_review triggered here):
- task_id: {blocked.task_id}
- title: {blocked.title}
- description: {blocked.description or '(not provided)'}
- validation_notes: {blocked.validation_notes or '(not provided)'}

COMPLETED TASKS (immutable — do not eliminate or modify):
{_render_completed(inp.completed_tasks)}

PENDING TASK TREE (candidates for elimination, modification, or replacement):
{_render_task_tree(inp.full_task_tree)}

Instructions:
1. Determine whether the clarification requires a narrow, moderate, or disruptive change.
2. For narrow: leave all task lists empty. If the blocked task only needs a retry
   (no elimination, no modifications), leave tasks_to_eliminate empty.
3. For moderate: identify exactly which tasks to eliminate (tasks_to_eliminate),
   which existing tasks need content changes (tasks_to_modify), and what new blocks
   of work are needed (new_work_blocks). Only include tasks actually affected.
4. For narrow or disruptive: leave tasks_to_eliminate, tasks_to_modify, new_work_blocks empty.
5. Provide clear reasoning for your scope decision.
6. Set updated_project_goal only if the project description itself must change.
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
        "impact_assessment_complete scope=%s eliminate=%d modify=%d new_blocks=%d",
        llm_output.change_scope,
        len(llm_output.tasks_to_eliminate),
        len(llm_output.tasks_to_modify),
        len(llm_output.new_work_blocks),
    )

    return ImpactAssessmentResult(
        change_scope=llm_output.change_scope,
        reasoning=llm_output.reasoning,
        tasks_to_eliminate=llm_output.tasks_to_eliminate,
        tasks_to_modify=llm_output.tasks_to_modify,
        new_work_blocks=llm_output.new_work_blocks,
        environment_changes=llm_output.environment_changes,
        updated_project_goal=llm_output.updated_project_goal,
    )
