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
from app.services.prompt_loader import prompt_loader

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

_SYSTEM_PROMPT = prompt_loader.get("confirmation_evaluator")


def _build_user_prompt(inp: ConfirmationEvaluatorInput) -> str:
    from app.services.prompt_loader import prompt_loader

    prompt_loader.validate_builder_inputs(
        "confirmation_evaluator",
        "main",
        {
            "action_summary": inp.action_summary,
            "user_response": inp.user_response,
        },
    )
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
