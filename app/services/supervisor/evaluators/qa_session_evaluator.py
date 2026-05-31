"""
Supervisor evaluator for QA session orchestration quality.

Assesses the QA Engine's session-level decisions across all completed sessions
for a project:
  - Did the orchestrator select the right agents for the product type?
  - Was risk coverage broad enough (multiple categories)?
  - Is the overall verdict well-calibrated vs. findings_summary severity counts?
  - Was the session efficient (duration vs. output)?

This evaluator reads *all* completed QA sessions (not filtered by a single
agent) and uses ``build_qa_session_evaluation_context`` from the shared helpers.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.evaluators._qa_evaluation_helpers import (
    build_qa_session_evaluation_context,
    get_qa_session_ids_analyzed,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = prompt_loader.get("qa_session_evaluator")

_AGENT_NAME = "qa_session"


def _build_user_prompt(*, project_id: int, ctx: dict) -> str:
    prompt_loader.validate_builder_inputs(
        "qa_session_evaluator",
        "main",
        {
            "project_id": project_id,
            "sessions": ctx["sessions"],
            "total_session_count": len(ctx["sessions"]),
        },
    )
    return (
        f"Evaluate the QA Engine's session orchestration for project {project_id}.\n\n"
        f"Completed QA sessions ({len(ctx['sessions'])} sessions):\n"
        f"{json.dumps(ctx['sessions'], ensure_ascii=False, indent=2)}\n\n"
        "Each session contains: session_id, product_type, verdict, agents_called, "
        "findings_summary, duration_seconds, findings (list).\n\n"
        "Assess agent selection completeness, risk coverage breadth, "
        "verdict calibration, and session efficiency."
    )


class QASessionEvaluator:
    """Evaluates session-level QA orchestration quality across all sessions."""

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
        ctx = build_qa_session_evaluation_context(db, project_id)

        if not ctx["sessions"]:
            return EvaluatorOutput(result=None)

        provider = get_llm_provider()
        user_prompt = _build_user_prompt(project_id=project_id, ctx=ctx)

        raw = provider.generate_structured(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="qa_session_evaluator_output",
            json_schema=AgentEvaluationOutput.model_json_schema(),
        )

        try:
            result = AgentEvaluationOutput.model_validate(raw)
        except ValidationError as exc:
            logger.warning("qa_session_evaluator_validation_failed exc=%s", exc)
            result = AgentEvaluationOutput(
                verdict="needs_attention",
                findings=f"Evaluator output validation failed: {exc}",
                issues=[],
                suggestions=[],
            )

        session_ids = get_qa_session_ids_analyzed(ctx)
        return EvaluatorOutput(result=result, execution_run_ids_analyzed=session_ids)
