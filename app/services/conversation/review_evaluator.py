"""Review Evaluator Agent.

During a manual_review episode, evaluates whether the user's accumulated responses
provide enough information to resume execution, or whether more clarification is
needed (up to the 3-attempt limit).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.services.conversation.requirements_evaluator import ConversationTurn
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema

logger = logging.getLogger(__name__)

REVIEW_ATTEMPTS_LIMIT = 3

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
    attempts_used: int


# ── LLM output schema ─────────────────────────────────────────────────────────


class ReviewEvaluatorLLMOutput(BaseModel):
    status: Literal["resolved", "insufficient", "abandoned"]
    next_question: str | None
    clarification_summary: str | None
    reasoning: str


# ── Result ────────────────────────────────────────────────────────────────────


@dataclass
class ReviewEvaluatorResult:
    status: Literal["resolved", "insufficient", "abandoned"]
    next_question: str | None
    clarification_summary: str | None
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

Your job is to evaluate the current conversation exchange and determine whether the user
has provided enough information to unblock the task.

Status "resolved": the user's responses, taken together, give a clear and actionable
direction to resolve the blocked task. Produce a clarification_summary that consolidates
ALL relevant information from the episode — this summary will be used by the system to
decide what changes to make. Be specific and complete.

Status "insufficient": the user's latest response does not yet provide enough information.
Produce a next_question that targets the single most important remaining gap.
Be empathetic — acknowledge what the user said before asking for more.

Status "abandoned": the user explicitly indicates they want to stop, skip, or cancel.
Examples: "forget it", "cancel", "abandon", "I don't know, just stop".

Rules:
- Never ask more than one question at a time.
- Do not ask for information already provided in the conversation.
- If the user expresses uncertainty but does not abandon, keep status "insufficient"
  and offer a concrete suggestion or explanation to help them decide.
- clarification_summary must be null when status is not "resolved".
- next_question must be null when status is "resolved" or "abandoned".

Return ONLY JSON matching the provided schema.
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
    return f"""
PROJECT GOAL:
{inp.project_goal}

BLOCKED TASK:
- task_id: {t.task_id}
- title: {t.title}
- description: {t.description or '(not provided)'}
- reason it was paused: {t.validation_notes or '(not provided)'}

REVIEW EPISODE CONVERSATION ({inp.attempts_used} user message(s) so far):
{_render_episode(inp.episode_history)}

Evaluate whether the user has provided enough information to resolve the blocked task.
Produce status, and either next_question (if insufficient) or clarification_summary (if resolved).
""".strip()


# ── Public API ────────────────────────────────────────────────────────────────


def evaluate_review(inp: ReviewEvaluatorInput) -> ReviewEvaluatorResult:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        ReviewEvaluatorLLMOutput.model_json_schema()
    )

    raw = provider.generate_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(inp),
        schema_name="review_evaluator_output",
        json_schema=strict_schema,
    )

    try:
        output = ReviewEvaluatorLLMOutput.model_validate(raw)
    except ValidationError as exc:
        raise ReviewEvaluatorError(
            f"ReviewEvaluator returned invalid structured output: {exc}"
        ) from exc

    logger.info(
        "review_evaluation_complete status=%s attempts_used=%s",
        output.status,
        inp.attempts_used,
    )

    return ReviewEvaluatorResult(
        status=output.status,
        next_question=output.next_question,
        clarification_summary=output.clarification_summary,
        reasoning=output.reasoning,
    )
