from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class AggregateRequest(BaseModel):
    version: str | None = Field(
        default=None,
        description="Exact git hash to match against system_versions_seen.",
    )
    date_from: datetime | None = None
    date_to: datetime | None = None
    project_ids: list[int] | None = Field(
        default=None,
        description="Explicit list of project IDs to include.",
    )
    limit: int | None = Field(
        default=None,
        ge=5,
        le=20,
        description="Cap on how many projects to include (5–20).",
    )
    include_dirty: bool | None = Field(
        default=None,
        description=(
            "Include dirty projects (no clean git hash). "
            "Defaults to False for version filters, True for date-only filters."
        ),
    )

    @model_validator(mode="after")
    def validate_filter(self) -> "AggregateRequest":
        has_filter = any(
            [
                self.version is not None,
                self.date_from is not None,
                self.date_to is not None,
                self.project_ids is not None,
                self.limit is not None,
            ]
        )
        if not has_filter:
            raise ValueError(
                "At least one filter must be specified: version, date_from, date_to, "
                "project_ids, or limit."
            )
        return self


class AgentFrequencyEntry(BaseModel):
    agent_name: str
    total_projects: int
    degraded_pct: int
    needs_attention_pct: int
    healthy_pct: int
    not_supervised_pct: int
    degraded_count: int
    needs_attention_count: int


class AggregateReportRead(BaseModel):
    aggregate_report_id: int
    created_at: datetime
    filter_params: dict | None
    supervisor_report_ids: list[int] | None
    project_ids_analyzed: list[int] | None
    dirty_projects_excluded: int
    agent_frequency_table: list[AgentFrequencyEntry] | None
    synthesis: str | None
    status: str
    error_message: str | None

    model_config = {"from_attributes": True}
