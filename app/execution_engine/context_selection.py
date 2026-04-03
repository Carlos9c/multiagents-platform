from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

HISTORICAL_SELECTION_RULE_SAME_FUNCTIONAL_SURFACE = "same_functional_surface"
HISTORICAL_SELECTION_RULE_SAME_WORK_STRATEGY = "same_work_strategy"
HISTORICAL_SELECTION_RULE_DIRECT_HISTORICAL_DEPENDENCY = "direct_historical_dependency"
HISTORICAL_SELECTION_RULE_REQUIRED_OPERATIONAL_CONTEXT = "required_operational_context"

VALID_HISTORICAL_SELECTION_RULES = {
    HISTORICAL_SELECTION_RULE_SAME_FUNCTIONAL_SURFACE,
    HISTORICAL_SELECTION_RULE_SAME_WORK_STRATEGY,
    HISTORICAL_SELECTION_RULE_DIRECT_HISTORICAL_DEPENDENCY,
    HISTORICAL_SELECTION_RULE_REQUIRED_OPERATIONAL_CONTEXT,
}


class HistoricalTaskRunSelection(BaseModel):
    task_id: int
    execution_run_id: int
    selection_rule: Literal[
        "same_functional_surface",
        "same_work_strategy",
        "direct_historical_dependency",
        "required_operational_context",
    ]
    selection_reason: str


class HistoricalTaskSelectionResult(BaseModel):
    selected_task_runs: list[HistoricalTaskRunSelection] = Field(default_factory=list)


class HistoricalTaskCatalogEntry(BaseModel):
    task_id: int
    execution_run_id: int

    title: str
    description: str | None = None
    summary: str | None = None
    objective: str | None = None

    task_type: str
    executor_type: str

    run_summary: str | None = None
    completed_scope: str | None = None
    validation_notes: str | None = None

    changed_files: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)


class ContextBuilderResult(BaseModel):
    should_invoke_context_selection_agent: bool
    completed_task_catalog: list[HistoricalTaskCatalogEntry] = Field(default_factory=list)
    project_context_excerpt: str | None = None
