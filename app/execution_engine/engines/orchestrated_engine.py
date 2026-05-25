from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.execution_engine.agent_runtime import StructuredLLMRuntime
from app.execution_engine.base import BaseExecutionEngine
from app.execution_engine.contracts import ExecutionRequest, ExecutionResult
from app.execution_engine.orchestrator import ExecutionOrchestrator
from app.execution_engine.subagent_registry import SubagentRegistry
from app.execution_engine.subagents.code_change_agent import CodeChangeAgent
from app.execution_engine.subagents.command_runner_agent import CommandRunnerAgent
from app.execution_engine.subagents.context_selection_agent import ContextSelectionAgent
from app.execution_engine.subagents.document_writer_agent import DocumentWriterAgent
from app.execution_engine.subagents.environment_manager_agent import EnvironmentManagerAgent
from app.execution_engine.subagents.test_builder_agent import TestBuilderAgent


class OrchestratedExecutionEngine(BaseExecutionEngine):
    backend_name = "orchestrated"

    def execute(
        self, db: Session, request: ExecutionRequest
    ) -> tuple[ExecutionResult, ExecutionRequest]:
        # Orchestrator and context-selection agent share the same runtime.
        orchestrator_runtime = StructuredLLMRuntime(
            model=settings.execution_engine_model,
            provider=settings.execution_engine_provider,
        )
        code_agent_runtime = StructuredLLMRuntime(
            model=settings.code_agent_model,
            provider=settings.code_agent_provider,
        )
        command_agent_runtime = StructuredLLMRuntime(
            model=settings.command_agent_model,
            provider=settings.command_agent_provider,
        )
        doc_writer_runtime = StructuredLLMRuntime(
            model=settings.documentation_agent_model,
            provider=settings.documentation_agent_provider,
        )
        test_agent_runtime = StructuredLLMRuntime(
            model=settings.test_agent_model,
            provider=settings.test_agent_provider,
        )

        registry = SubagentRegistry(
            subagents=[
                ContextSelectionAgent(runtime=orchestrator_runtime),
                CodeChangeAgent(runtime=code_agent_runtime),
                CommandRunnerAgent(runtime=command_agent_runtime),
                DocumentWriterAgent(runtime=doc_writer_runtime),
                TestBuilderAgent(runtime=test_agent_runtime),
                EnvironmentManagerAgent(runtime=orchestrator_runtime),
            ]
        )

        orchestrator = ExecutionOrchestrator(
            runtime=orchestrator_runtime,
            registry=registry,
            budget=self.budget,
        )
        return orchestrator.run(db, request)
