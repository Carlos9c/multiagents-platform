"""Review Evaluator Agent.

During a manual_review episode, evaluates whether the user's accumulated responses
provide enough information to resume execution. There is no attempt limit — the
conversation continues until:
  - Aria has enough context to propose a concrete plan ("ready_to_confirm")
  - The user explicitly abandons the task ("abandoned")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.services.conversation.impact_assessment_agent import TaskContextForReview
from app.services.conversation.requirements_evaluator import ConversationTurn
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)


# ── Input ─────────────────────────────────────────────────────────────────────


@dataclass
class BlockedTaskContext:
    task_id: int
    title: str
    description: str | None
    validation_notes: str | None


@dataclass
class ReviewEvaluatorInput:
    project_goal: str
    blocked_task: BlockedTaskContext
    episode_history: list[ConversationTurn]
    project_task_summary: str | None = None
    # True when the "blocked task" is a synthetic wrapper around a project-level
    # infrastructure/configuration failure (no real task exists). The evaluator must
    # focus on infrastructure recovery options rather than code-task clarification.
    is_project_level_error: bool = False
    # Detected output language — if provided, injected as OUTPUT LANGUAGE prefix.
    language: str | None = None
    # Full pending task tree with hierarchy and dependency information.
    # Enables cascade-aware responses: when a user wants to eliminate a task,
    # Aria can surface which dependents would also be affected and propose alternatives.
    full_task_tree: list[TaskContextForReview] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.full_task_tree is None:
            self.full_task_tree = []


# ── LLM output schema ─────────────────────────────────────────────────────────


class ReviewEvaluatorLLMOutput(BaseModel):
    status: Literal["ready_to_confirm", "insufficient", "abandoned"]
    next_question: str | None
    clarification_summary: str | None
    action_summary: str | None
    reasoning: str


# ── Result ────────────────────────────────────────────────────────────────────


@dataclass
class ReviewEvaluatorResult:
    status: Literal["ready_to_confirm", "insufficient", "abandoned"]
    next_question: str | None
    clarification_summary: str | None
    action_summary: str | None
    reasoning: str


class ReviewEvaluatorError(Exception):
    pass


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = prompt_loader.get("review_evaluator")


def _render_task_tree(tasks: list[TaskContextForReview]) -> str:
    if not tasks:
        return "(none)"
    lines = []
    for t in tasks:
        parent_info = f" [parent: {t.parent_title}]" if t.parent_title else ""
        deps = ", ".join(t.depends_on_task_titles) if t.depends_on_task_titles else "none"
        order = t.sequence_order if t.sequence_order is not None else "?"
        lines.append(f"  - [{t.task_id}] (order={order}, level={t.planning_level}){parent_info}")
        lines.append(f"    title: {t.title}")
        lines.append(f"    status: {t.status} | depends_on: {deps}")
    return "\n".join(lines)


def _render_episode(history: list[ConversationTurn]) -> str:
    if not history:
        return "(episode just started — no exchanges yet)"
    lines = []
    for turn in history:
        prefix = "User" if turn.role == "user" else "Aria"
        lines.append(f"{prefix}: {turn.content}")
    return "\n".join(lines)


def _build_user_prompt(inp: ReviewEvaluatorInput) -> str:
    from app.services.prompt_loader import prompt_loader

    prompt_loader.validate_builder_inputs(
        "review_evaluator",
        "main",
        {
            "project_goal": inp.project_goal,
            "blocked_task": inp.blocked_task,
            "project_task_summary": inp.project_task_summary,
            "episode_history": inp.episode_history,
            "is_project_level_error": inp.is_project_level_error,
            "language": inp.language,
            "full_task_tree": inp.full_task_tree,
        },
    )
    lang_prefix = f"OUTPUT LANGUAGE: {inp.language}.\n\n" if inp.language else ""
    t = inp.blocked_task

    task_progress_section = ""
    if inp.project_task_summary:
        task_progress_section = f"""

PROJECT TASK PROGRESS:
{inp.project_task_summary}
"""

    if inp.is_project_level_error:
        blocked_section = f"""
[NOTA: Este no es un error de tarea de código. Es un fallo a nivel de proyecto
(infraestructura, configuración, planificación). La «tarea bloqueada» a continuación
describe el error del sistema y las opciones de recuperación disponibles.
Tu objetivo es entender qué cambios de configuración o dirección quiere el usuario
para que el flujo de trabajo pueda reiniciarse correctamente.]

ERROR DE PROYECTO:
- tipo: {t.title}
- descripción técnica: {t.description or '(no proporcionada)'}
- detalles del error: {t.validation_notes or '(no proporcionados)'}
"""
    else:
        blocked_section = f"""
BLOCKED TASK:
- task_id: {t.task_id}
- title: {t.title}
- description: {t.description or '(not provided)'}
- reason it was paused: {t.validation_notes or '(not provided)'}
"""

    task_tree_section = ""
    if inp.full_task_tree:
        task_tree_section = f"""

PENDING TASK TREE:
{_render_task_tree(inp.full_task_tree)}
"""

    return f"""{lang_prefix}PROJECT GOAL:
{inp.project_goal}
{task_progress_section}{blocked_section}{task_tree_section}
REVIEW EPISODE CONVERSATION:
{_render_episode(inp.episode_history)}

Evaluate whether you have enough information to propose a concrete action plan.
If yes, produce status="ready_to_confirm" with clarification_summary and action_summary.
If not, produce status="insufficient" with next_question.
""".strip()


# ── Public API ────────────────────────────────────────────────────────────────


def evaluate_review(inp: ReviewEvaluatorInput) -> ReviewEvaluatorResult:
    provider = get_llm_provider()

    raw = provider.generate_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(inp),
        schema_name="review_evaluator_output",
        json_schema=ReviewEvaluatorLLMOutput.model_json_schema(),
    )

    try:
        output = ReviewEvaluatorLLMOutput.model_validate(raw)
    except ValidationError as exc:
        raise ReviewEvaluatorError(
            f"ReviewEvaluator returned invalid structured output: {exc}"
        ) from exc

    logger.info("review_evaluation_complete status=%s", output.status)

    return ReviewEvaluatorResult(
        status=output.status,
        next_question=output.next_question,
        clarification_summary=output.clarification_summary,
        action_summary=output.action_summary,
        reasoning=output.reasoning,
    )
