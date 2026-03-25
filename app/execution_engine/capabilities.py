from __future__ import annotations

from pydantic import BaseModel

from app.models.task import EXECUTION_ENGINE, normalize_executor_type


class ExecutorCapabilities(BaseModel):
    executor_type: str
    supports_artifact_creation: bool
    supports_artifact_modification: bool
    supports_bootstrap_from_empty_workspace: bool
    requires_workspace: bool = True


def get_execution_engine_capabilities() -> ExecutorCapabilities:
    """
    Canonical capability profile for the current execution engine.

    This profile should describe what the active orchestrated execution system
    can reliably do through its current subagents and tools.
    """
    return ExecutorCapabilities(
        executor_type=EXECUTION_ENGINE,
        supports_artifact_creation=True,
        supports_artifact_modification=True,
        supports_bootstrap_from_empty_workspace=True,
        requires_workspace=True,
    )


def get_executor_capabilities(executor_type: str | None) -> ExecutorCapabilities:
    """
    Return the capability profile for the given executor type.

    Rules:
    - normalize legacy aliases before deciding
    - execution_engine is the only active canonical executor profile
    - unknown values fall back to a conservative profile
    """
    normalized_executor_type = normalize_executor_type(executor_type)

    if normalized_executor_type == EXECUTION_ENGINE:
        return get_execution_engine_capabilities()

    return ExecutorCapabilities(
        executor_type=normalized_executor_type or "unknown",
        supports_artifact_creation=False,
        supports_artifact_modification=False,
        supports_bootstrap_from_empty_workspace=False,
        requires_workspace=True,
    )