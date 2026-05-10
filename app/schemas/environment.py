from typing import Literal

from pydantic import BaseModel, Field

RuntimeType = Literal["python_venv", "node_npm", "rust_cargo"]


class EnvVar(BaseModel):
    key: str = Field(min_length=1, description="Environment variable name.")
    value: str = Field(description="Environment variable value.")


class LLMPinnedDependency(BaseModel):
    name: str = Field(
        min_length=1,
        description="Package distribution name, e.g. 'xgboost', 'scikit-learn'.",
    )
    version: str = Field(
        min_length=1,
        description="Exact version string, e.g. '2.1.3'. No ranges or wildcards.",
    )
    extras: list[str] = Field(
        default_factory=list,
        description="Optional PEP 508 extras, e.g. ['sklearn'] for xgboost[sklearn].",
    )
    purpose: str = Field(
        min_length=5,
        description="Why this package is needed for the project.",
    )


class RuntimeEnvironmentPlanOutput(BaseModel):
    runtime_type: RuntimeType
    image: str = Field(
        min_length=5,
        description=(
            "Docker image to use, e.g. 'python:3.12-slim'. "
            "Choose official, well-maintained images."
        ),
    )
    dependencies: list[LLMPinnedDependency]
    environment_variables: list[EnvVar] = Field(
        default_factory=list,
        description="Additional environment variables to set in the container.",
    )
    planning_rationale: str = Field(
        min_length=30,
        description="Explanation of why this runtime and dependencies were chosen.",
    )


class RuntimeSpecRepairOutput(BaseModel):
    corrected_dependencies: list[LLMPinnedDependency]
    corrected_image: str = Field(
        min_length=5,
        description="Docker image to use after repair (may be the same as before).",
    )
    repair_rationale: str = Field(
        min_length=20,
        description="Explanation of what was wrong and what was changed.",
    )
