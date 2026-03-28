from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CodeValidationFinding(BaseModel):
    severity: Literal["info", "warning", "error"]
    category: str = Field(..., min_length=3)
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)


class CodeValidationLLMOutput(BaseModel):
    decision: Literal["completed", "partial", "failed", "manual_review"]
    summary: str
    validated_scope: str | None = None
    missing_scope: str | None = None
    blockers: list[str] = Field(default_factory=list)
    findings: list[CodeValidationFinding] = Field(default_factory=list)
    manual_review_required: bool = False
    confidence: Literal["high", "medium", "low"]
    reasoning_notes: list[str] = Field(default_factory=list)