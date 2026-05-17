"""Requirements Evaluator Agent.

Given the full conversation history and the current requirements draft,
decides whether enough information exists to start the project or
whether more clarification is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.services.llm.factory import get_llm_provider

logger = logging.getLogger(__name__)

REVIEW_ATTEMPTS_LIMIT = 3

# ── Input ─────────────────────────────────────────────────────────────────────


@dataclass
class ConversationTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass
class RequirementsEvaluatorInput:
    project_name: str
    history: list[ConversationTurn]
    current_draft: str | None = None


# ── LLM output schema ─────────────────────────────────────────────────────────


class RequirementsEvaluatorLLMOutput(BaseModel):
    status: Literal["sufficient", "needs_more"]
    next_question: str | None
    updated_draft: str | None
    reasoning: str


# ── Result ────────────────────────────────────────────────────────────────────


@dataclass
class RequirementsEvaluatorResult:
    status: Literal["sufficient", "needs_more"]
    next_question: str | None
    updated_draft: str | None
    reasoning: str


class RequirementsEvaluatorError(Exception):
    pass


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are a project requirements assistant for an autonomous software development system.
Your name is Aria. You are warm, precise, and professionally cheerful.

Your task is to evaluate a conversation with a user who wants to build a software project,
and determine whether you have enough information to generate a complete project description
that will drive autonomous development.

A complete project description must cover:
1. WHAT to build — the core functionality, main features, user-facing behaviour
2. HOW to build it — technology stack, architecture constraints, key implementation decisions
3. QUALITY GATES — what "done" looks like: tests, validation rules, acceptance criteria
4. BOUNDARIES — what is explicitly out of scope

You interact with the user to extract this information conversationally. Each exchange should
clarify one meaningful gap. Do not ask multiple questions at once. Do not ask about things
already covered. Do not ask unnecessary questions — if something can be reasonably assumed
(e.g. standard Python project structure), don't ask.

When status is "needs_more":
- next_question: a single, concrete, friendly question that fills the most important gap
- updated_draft: an updated partial draft incorporating everything clarified so far
  (even if incomplete — track progress). Write it in the same style as a technical description.

When status is "sufficient":
- next_question: null
- updated_draft: the COMPLETE, final project description. This will be used verbatim as the
  project goal for autonomous development. It must be detailed, precise, and unambiguous.
  Include all technical decisions, domain rules, acceptance criteria, and constraints.
  Write it in third person ("The system must...", "The application must...").

Return ONLY JSON matching the provided schema.
""".strip()

_ASSISTANT_INTRO = (
    "¡Hola! Soy Aria, tu asistente de proyectos. "
    "Cuéntame qué quieres construir y te haré las preguntas necesarias para "
    "entender bien el proyecto antes de que empiece el desarrollo."
)


def _render_history(history: list[ConversationTurn]) -> str:
    if not history:
        return "(no messages yet)"
    lines = []
    for turn in history:
        prefix = "User" if turn.role == "user" else "Aria"
        lines.append(f"{prefix}: {turn.content}")
    return "\n".join(lines)


def _build_user_prompt(inp: RequirementsEvaluatorInput) -> str:
    draft_section = (
        f"\nCURRENT REQUIREMENTS DRAFT:\n{inp.current_draft}"
        if inp.current_draft
        else "\nCURRENT REQUIREMENTS DRAFT: (none yet)"
    )
    return f"""
Project name: {inp.project_name}
{draft_section}

CONVERSATION HISTORY:
{_render_history(inp.history)}

Evaluate whether the conversation contains enough information to produce a complete
project description. If not, identify the single most important missing piece and ask for it.
If yes, produce the final complete description as updated_draft.
""".strip()


# ── Public API ────────────────────────────────────────────────────────────────


def get_intro_message() -> str:
    return _ASSISTANT_INTRO


def evaluate_requirements(
    inp: RequirementsEvaluatorInput,
) -> RequirementsEvaluatorResult:
    provider = get_llm_provider()

    raw = provider.generate_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(inp),
        schema_name="requirements_evaluator_output",
        json_schema=RequirementsEvaluatorLLMOutput.model_json_schema(),
    )

    try:
        output = RequirementsEvaluatorLLMOutput.model_validate(raw)
    except ValidationError as exc:
        raise RequirementsEvaluatorError(
            f"RequirementsEvaluator returned invalid structured output: {exc}"
        ) from exc

    logger.info(
        "requirements_evaluation_complete status=%s draft_len=%s",
        output.status,
        len(output.updated_draft or ""),
    )

    return RequirementsEvaluatorResult(
        status=output.status,
        next_question=output.next_question,
        updated_draft=output.updated_draft,
        reasoning=output.reasoning,
    )
