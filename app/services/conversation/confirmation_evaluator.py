"""Confirmation Evaluator Agent.

After Aria has gathered enough information and proposed an action plan, this
evaluator determines whether the user is confirming the plan or providing
additional clarification/modifications that require further discussion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from app.services.llm.factory import get_llm_provider

logger = logging.getLogger(__name__)


# ── Input / Output ────────────────────────────────────────────────────────────


@dataclass
class ConfirmationEvaluatorInput:
    action_summary: str  # The plan Aria proposed
    user_response: str  # Latest user message


class ConfirmationEvaluatorLLMOutput(BaseModel):
    confirmed: bool
    follow_up: str | None  # If not confirmed: Aria's next message to continue discussion
    reasoning: str


@dataclass
class ConfirmationEvaluatorResult:
    confirmed: bool
    follow_up: str | None
    reasoning: str


class ConfirmationEvaluatorError(Exception):
    pass


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are Aria, a project assistant for an autonomous software development system.
You are warm, precise, and professionally cheerful.

You previously gathered enough context from the user to resolve a blocked development
task, and you proposed a concrete action plan. Your job now is to determine whether
the user's response is a confirmation to proceed, or whether they want to change or
clarify something first.

confirmed=true: The user agrees, approves, or tells you to go ahead. This includes:
  - Direct affirmations: "Yes", "Go ahead", "Sounds good", "Ok", "Do it", "Perfect",
    "Sure", "That's right", "Adelante", "Sí", "Perfecto", "De acuerdo", etc.
  - Any response that is generally positive without requesting changes.

confirmed=false: The user wants to modify the plan, asks a clarifying question,
  expresses doubt, or provides new information. In this case, produce a follow_up
  message that warmly acknowledges their input and continues the discussion.

Rules:
- Be generous in interpreting confirmation — if the response is broadly positive,
  set confirmed=true even if phrased casually.
- When confirmed=false, follow_up must be a natural, empathetic continuation that
  acknowledges exactly what the user said and asks the most important next question.
- When confirmed=true, follow_up must be null.
- Do not ask for re-confirmation if the user already confirmed — that wastes their time.

Language rule:
- Detect the language of the user's response.
- Write follow_up in that same language.
- Never mix languages within a single response.
""".strip()


def _build_user_prompt(inp: ConfirmationEvaluatorInput) -> str:
    return f"""ACTION PLAN ARIA PROPOSED:
{inp.action_summary}

USER RESPONSE:
{inp.user_response}

Is the user confirming the plan, or providing further input?
""".strip()


# ── Public API ────────────────────────────────────────────────────────────────


def evaluate_confirmation(inp: ConfirmationEvaluatorInput) -> ConfirmationEvaluatorResult:
    provider = get_llm_provider()

    raw = provider.generate_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(inp),
        schema_name="confirmation_evaluator_output",
        json_schema=ConfirmationEvaluatorLLMOutput.model_json_schema(),
    )

    try:
        output = ConfirmationEvaluatorLLMOutput.model_validate(raw)
    except ValidationError as exc:
        raise ConfirmationEvaluatorError(
            f"ConfirmationEvaluator returned invalid structured output: {exc}"
        ) from exc

    logger.info("confirmation_evaluation_complete confirmed=%s", output.confirmed)

    return ConfirmationEvaluatorResult(
        confirmed=output.confirmed,
        follow_up=output.follow_up,
        reasoning=output.reasoning,
    )
