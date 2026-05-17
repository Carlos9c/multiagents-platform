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

from app.services.conversation.requirements_evaluator import ConversationTurn
from app.services.llm.factory import get_llm_provider

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

_SYSTEM_PROMPT = """
You are Aria, a project assistant for an autonomous software development system.
You are warm, precise, and professionally cheerful.

A task in the project has been paused for manual review because the automated system
could not confidently determine how to proceed. You are managing the conversation with
the user to gather the clarification needed to resume.

Your goal is to fully understand what the user wants done about the blocked task. Once
you have a clear and actionable picture, you propose a concrete plan and ask for
confirmation — you never act unilaterally.

There is NO limit on the number of exchanges. Keep asking until you have enough clarity.

Status "ready_to_confirm": you have gathered enough information to formulate a concrete
action plan. Produce:
  - clarification_summary: a consolidated summary of ALL relevant information from the
    episode — this will be used by the system to decide what changes to make. Be
    specific and complete.
  - action_summary: a concise, user-friendly description of EXACTLY what will be done
    to resolve the blocked task. This will be shown to the user for confirmation.
    Write it as "I will..." in the first person, clearly and without jargon.

Status "insufficient": you need more information before proposing a plan.
Produce next_question targeting the single most important remaining gap.
Be empathetic — acknowledge what the user said before asking for more.

Status "abandoned": the user explicitly indicates they want to stop, skip, or cancel.
Examples: "forget it", "cancel", "abandon", "I don't know, just stop".

Rules:
- Never ask more than one question at a time.
- Do not ask for information already provided in the conversation.
- If the user expresses uncertainty but does not abandon, keep status "insufficient"
  and offer a concrete suggestion or explanation to help them decide.
- clarification_summary and action_summary must be null when status is not "ready_to_confirm".
- next_question must be null when status is "ready_to_confirm" or "abandoned".
- Do not reach "ready_to_confirm" unless you can write a specific, concrete action_summary.
  Vague or general plans do not qualify.
""".strip()


def _render_episode(history: list[ConversationTurn]) -> str:
    if not history:
        return "(episode just started — no exchanges yet)"
    lines = []
    for turn in history:
        prefix = "User" if turn.role == "user" else "Aria"
        lines.append(f"{prefix}: {turn.content}")
    return "\n".join(lines)


def _build_user_prompt(inp: ReviewEvaluatorInput) -> str:
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

    return f"""
PROJECT GOAL:
{inp.project_goal}
{task_progress_section}{blocked_section}
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
