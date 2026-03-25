from __future__ import annotations

from pydantic import BaseModel


class ExecutorCapabilities(BaseModel):
    executor_type: str
    supports_artifact_creation: bool
    supports_artifact_modification: bool
    supports_bootstrap_from_empty_workspace: bool
    requires_workspace: bool = True


def get_executor_capabilities(executor_type: str) -> ExecutorCapabilities:
    """
    Generic capability profile for the current executor.

    This is deliberately executor-driven, not task-type-driven.
    The orchestrator should reason from what the executor can do,
    not from assumptions about documentation/code/design categories.
    """
    if executor_type == "code_executor":
        return ExecutorCapabilities(
            executor_type=executor_type,
            supports_artifact_creation=True,
            supports_artifact_modification=True,
            supports_bootstrap_from_empty_workspace=True,
            requires_workspace=True,
        )

    return ExecutorCapabilities(
        executor_type=executor_type,
        supports_artifact_creation=False,
        supports_artifact_modification=False,
        supports_bootstrap_from_empty_workspace=False,
        requires_workspace=True,
    )