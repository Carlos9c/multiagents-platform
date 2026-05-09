from __future__ import annotations

from pydantic import BaseModel


class PlanHistoryCycleRead(BaseModel):
    planned_at: str
    objective: str
    source_path: str | None = None
