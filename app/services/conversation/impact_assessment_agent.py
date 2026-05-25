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

_SYSTEM_PROMPT = """
You are the Impact Assessment Agent for an autonomous software development system.

Your job is to analyse a user clarification received after a task was paused for manual review,
and determine what scope of change is required to resume project execution.

You classify the clarification into one of three scopes:

NARROW — the clarification only fixes the specific blocked task without invalidating
anything else already planned or completed. Use this when:
- The user corrects a small misunderstanding or provides a missing detail
- The high-level plan and all other tasks remain fully valid
- Completed work is unaffected

MODERATE — the clarification affects multiple atomic tasks but the high-level goals
remain valid. Use this when:
- Some pending atomic tasks need updated descriptions, steps, or criteria
- New atomic tasks may be needed to cover work not previously planned
- Completed work is still structurally valid
- The high-level tasks (objectives) themselves do NOT change

DISRUPTIVE — the clarification fundamentally changes the direction of the project.
Use this when:
- At least one high-level goal/objective is no longer valid
- The clarification invalidates completed work (not just pending tasks)
- A major technology or architecture decision changes
- The overall project description must be rewritten

Rules:
- Be conservative: prefer narrow over moderate, moderate over disruptive.
- Only classify as disruptive if there is a clear invalidation of high-level goals
  or completed work.
- For moderate scope: produce concrete task_id-level revisions. Only modify tasks
  where you can identify specific changes required by the clarification. Leave
  unaffected tasks out of tasks_to_modify.
- For narrow or disruptive scope: leave tasks_to_modify and tasks_to_add empty.
- reasoning must explain WHY you chose this scope and what specifically is affected.
- resequence_needed: set true for moderate when the new/modified tasks change the
  logical ordering of pending work.
- environment_changes: populate this list if the clarification implies new libraries,
  frameworks, tools, or runtime dependencies that are not already part of the project.
  This applies to ALL scopes (narrow, moderate, disruptive). Leave empty if no new
  dependencies are required. Each entry needs:
  * package_name: the installable package name.
  * version_constraint: the version string (e.g. "2.31.0") or null if any version is fine.
  * reason: why this dependency is needed.
  * version_strictness: how strictly to enforce the version ("exact_only" | "preferred" |
    "any_compatible"). Use "exact_only" when the user explicitly requires a specific version
    and it is not negotiable. Use "preferred" when a version is mentioned but alternatives
    are acceptable. Use "any_compatible" (default) when no version is specified.

Return ONLY JSON matching the provided schema.
""".strip()


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
