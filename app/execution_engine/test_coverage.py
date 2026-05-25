from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TestCoverageObservation(BaseModel):
    """
    Structured observation produced by test_builder_agent alongside the test
    files it materializes.

    This is an LLM output schema. All fields are required in the JSON output.
    Nullable fields accept null. List fields accept empty arrays.

    Design contract:
    - Describes what the generated tests cover and what gaps remain.
    - Documents observations about the implementation without prescribing fixes.
    - The orchestrator reads potential_implementation_gaps via evidence observations
      to decide whether a code repair pass is needed before or after test generation.
    """

    covered_cases: list[str] = Field(default_factory=list)
    uncovered_cases: list[str] = Field(default_factory=list)
    tested_against: Literal["acceptance_criteria", "observed_implementation", "both"]
    potential_implementation_gaps: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
