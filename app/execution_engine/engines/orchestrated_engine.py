from __future__ import annotations

from app.core.config import settings
from app.execution_engine.agent_runtime import StructuredLLMRuntime
from app.execution_engine.base import BaseExecutionEngine
from app.execution_engine.contracts import ExecutionRequest, ExecutionResult
from app.execution_engine.orchestrator import ExecutionOrchestrator
from app.execution_engine.subagent_registry import SubagentRegistry
from app.execution_engine.subagents.code_change_agent import CodeChangeAgent
from app.execution_engine.subagents.command_runner_agent import CommandRunnerAgent
from app.execution_engine.subagents.context_selection_agent import ContextSelectionAgent
from app.execution_engine.subagents.placement_resolver_agent import (
    PlacementResolverAgent,
)


class OrchestratedExecutionEngine(BaseExecutionEngine):
    backend_name = "orchestrated"

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        runtime = StructuredLLMRuntime(model=settings.execution_engine_model)

        registry = SubagentRegistry(
            subagents=[
                ContextSelectionAgent(runtime=runtime),
                PlacementResolverAgent(runtime=runtime),
                CodeChangeAgent(runtime=runtime),
                CommandRunnerAgent(),
            ]
        )

        orchestrator = ExecutionOrchestrator(
            runtime=runtime,
            registry=registry,
            budget=self.budget,
        )
        return orchestrator.run(request)
