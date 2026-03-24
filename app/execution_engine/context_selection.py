from __future__ import annotations

from pydantic import BaseModel, Field


class ContextFileSelection(BaseModel):
    path: str
    reason: str
    relevance: float
    include_full_content: bool = False
    symbol_hints: list[str] = Field(default_factory=list)


class ContextSelectionResult(BaseModel):
    summary: str
    files: list[ContextFileSelection] = Field(default_factory=list)
    related_task_ids: list[int] = Field(default_factory=list)
    architectural_notes: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)