"""
Supervisor evaluator for the boundary_qa_agent.

Reads QA session records where boundary_qa_agent ran and assesses how well the
agent applied the deterministic boundary corpus: corpus coverage, finding
precision, and whether reported findings are genuinely boundary-condition issues.
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
    build_qa_agent_evaluation_context,
    get_qa_session_ids_analyzed,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = prompt_loader.get("boundary_qa_agent_evaluator")

_AGENT_NAME = "boundary_qa_agent"


def _build_user_prompt(*, project_id: int, ctx: dict) -> str:
    prompt_loader.validate_builder_inputs(
        "boundary_qa_agent_evaluator",
        "main",
        {
            "project_id": project_id,
            "agent_name": _AGENT_NAME,
            "sessions": ctx["sessions"],
            "total_session_count": len(ctx["sessions"]),
        },
    )
    return (
        f"Evaluate the {_AGENT_NAME}'s performance for project {project_id}.\n\n"
        f"QA sessions analysed ({len(ctx['sessions'])} sessions):\n"
        f"{json.dumps(ctx['sessions'], ensure_ascii=False, indent=2)}\n\n"
        "Each session contains: session_id, product_type, verdict, agents_called, "
        "findings (with category='boundary'), findings_summary.\n\n"
        f"Assess corpus coverage discipline and finding precision for the {_AGENT_NAME}."
    )


class BoundaryQAAgentEvaluator:
    """Evaluates the boundary_qa_agent's corpus coverage and finding quality."""

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
        ctx = build_qa_agent_evaluation_context(db, project_id, agent_name=_AGENT_NAME)

        if not ctx["sessions"]:
            return EvaluatorOutput(result=None)

        provider = get_llm_provider()
        user_prompt = _build_user_prompt(project_id=project_id, ctx=ctx)

        raw = provider.generate_structured(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="boundary_qa_agent_evaluator_output",
            json_schema=AgentEvaluationOutput.model_json_schema(),
        )

        try:
            result = AgentEvaluationOutput.model_validate(raw)
        except ValidationError as exc:
            logger.warning("boundary_qa_agent_evaluator_validation_failed exc=%s", exc)
            result = AgentEvaluationOutput(
                verdict="needs_attention",
                findings=f"Evaluator output validation failed: {exc}",
                issues=[],
                suggestions=[],
            )

        session_ids = get_qa_session_ids_analyzed(ctx)
        return EvaluatorOutput(result=result, execution_run_ids_analyzed=session_ids)
