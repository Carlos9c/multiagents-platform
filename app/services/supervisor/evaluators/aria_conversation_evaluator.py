"""
Supervisor evaluator for the Aria conversational orchestrator and project_query_agent.

Assesses response quality, query factual accuracy (using task_counts as ground truth),
conversational coherence, phase-appropriate behavior, and language consistency.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.evaluators._conversation_evaluation_helpers import (
    load_aria_conversation_context,
)
from app.services.supervisor.prompt_resolver import resolve_system_prompt

logger = logging.getLogger(__name__)

ARIA_CONVERSATION_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("aria_conversation_evaluator")

_AGENT_NAME = "aria_orchestrator"
_QUERY_AGENT_NAME = "project_query_agent"


def _build_user_prompt(
    *,
    project_id: int,
    aria_system_prompt: str,
    query_agent_system_prompt: str,
    conversation_context: dict,
) -> str:
    total_message_count = conversation_context.get("message_count", 0)
    total_query_count = len(conversation_context.get("project_queries", []))

    prompt_loader.validate_builder_inputs(
        "aria_conversation_evaluator",
        "main",
        {
            "project_id": project_id,
            "aria_system_prompt": aria_system_prompt,
            "query_agent_system_prompt": query_agent_system_prompt,
            "conversation_context": conversation_context,
            "total_message_count": total_message_count,
            "total_query_count": total_query_count,
        },
    )
    return f"""
Evaluate the conversational quality of the Aria agent for project {project_id}.

Aria orchestrator system prompt:
---
{aria_system_prompt}
---

Project query agent system prompt:
---
{query_agent_system_prompt}
---

Conversation context ({total_message_count} messages, {total_query_count} query records):
{json.dumps(conversation_context, ensure_ascii=False, indent=2)}

The conversation_context contains: final_phase, review_episode_count,
requirements_draft_preview, messages (role/content pairs), and project_queries
(structured records with user_question, aria_response, and task_counts ground truth).

Assess response quality, query factual accuracy against task_counts, conversational
coherence, phase-appropriate behavior, and language consistency.
Reference specific message roles and query_index values when raising issues.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "aria_conversation_evaluator",
        "retry",
        {
            "project_id": project_id,
            "validation_error": validation_error,
        },
    )
    return f"""
Your previous response for project_id={project_id} was invalid.

Validation error:
{validation_error}

Return valid JSON with all required fields:
- verdict: one of "healthy", "needs_attention", or "degraded"
- findings: prose description (minimum 20 characters)
- issues: list of specific problems (empty list if none)
- suggestions: list of actionable suggestions (empty list if none)
""".strip()


class AriaConversationEvaluator:
    """Evaluates the conversational quality of the Aria orchestrator for a project."""

    AGENT_NAME = _AGENT_NAME

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        conversation_context = load_aria_conversation_context(db, project_id)

        if conversation_context is None or conversation_context.get("message_count", 0) == 0:
            return EvaluatorOutput(result=None)

        aria_system_prompt = resolve_system_prompt(_AGENT_NAME, system_version=system_version)
        query_agent_system_prompt = resolve_system_prompt(
            _QUERY_AGENT_NAME, system_version=system_version
        )
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            aria_system_prompt=aria_system_prompt,
            query_agent_system_prompt=query_agent_system_prompt,
            conversation_context=conversation_context,
        )

        raw = provider.generate_structured(
            system_prompt=ARIA_CONVERSATION_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="aria_conversation_evaluator_output",
            json_schema=AgentEvaluationOutput.model_json_schema(),
        )

        try:
            result = AgentEvaluationOutput.model_validate(raw)
        except ValidationError as exc:
            retry_prompt = _build_retry_prompt(
                project_id=project_id,
                validation_error=str(exc),
            )
            raw_retry = provider.generate_structured(
                system_prompt=ARIA_CONVERSATION_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="aria_conversation_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(result=result)
