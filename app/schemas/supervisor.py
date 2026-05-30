from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AgentEvaluationRead(BaseModel):
    agent_name: str
    verdict: str | None
    findings: str | None
    issues: list[str] | None
    suggestions: list[str] | None
    execution_run_ids_analyzed: list[int] | None
    system_versions_seen: list[str] | None

    model_config = {"from_attributes": True}


class SupervisorReportRead(BaseModel):
    report_id: int
    project_id: int
    status: str
    overall_verdict: str | None
    synthesis: str | None
    analyzed_run_ids: list[int] | None
    error_message: str | None
    created_at: datetime | None
    completed_at: datetime | None
    agent_evaluations: list[AgentEvaluationRead]

    model_config = {"from_attributes": True}
