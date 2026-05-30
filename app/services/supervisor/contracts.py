"""
Shared contracts for Supervisor evaluator agents.

Each evaluator returns an EvaluatorOutput which wraps an AgentEvaluationOutput
(the LLM result) with metadata about which ExecutionRuns were analyzed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AgentVerdict = Literal["healthy", "needs_attention", "degraded"]


class AgentEvaluationOutput(BaseModel):
    """LLM output schema for all evaluators. Used as the structured-output schema."""

    verdict: AgentVerdict
    findings: str = Field(
        description="Prose synthesis of the agent's performance across the project.",
        min_length=20,
    )
    issues: list[str] = Field(
        default_factory=list,
        description="Concrete, specific problems identified. Empty list if healthy.",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Actionable improvement suggestions. May be empty.",
    )


class EvaluatorOutput(BaseModel):
    """Wrapper returned by every evaluator's evaluate() method.

    result=None means the agent did not participate in this project (not_supervised).
    The runner maps None to verdict=NULL in AgentEvaluation.
    """

    result: AgentEvaluationOutput | None
    execution_run_ids_analyzed: list[int] = Field(default_factory=list)
    system_versions_seen: list[str] = Field(default_factory=list)
