from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.execution_engine.base import BaseExecutionEngine
from app.execution_engine.budget import LoopBudget
from app.execution_engine.engines.documentation_engine import DocumentationEngine
from app.execution_engine.engines.orchestrated_engine import OrchestratedExecutionEngine
from app.models.task import DOCUMENTATION_ENGINE


def build_default_loop_budget() -> LoopBudget:
    return LoopBudget(
        max_steps=settings.execution_engine_max_steps,
        max_agent_calls=settings.execution_engine_max_agent_calls,
        max_repair_attempts=settings.execution_engine_max_repair_attempts,
    )


def get_engine_for_executor_type(executor_type: str) -> object:
    if executor_type == DOCUMENTATION_ENGINE:
        return DocumentationEngine()

    backend = settings.execution_engine_backend
    budget = build_default_loop_budget()

    if backend == "orchestrated":
        return OrchestratedExecutionEngine(budget=budget)

    raise ValueError(
        f"Unsupported execution_engine_backend '{backend}'. Supported backends: ['orchestrated']"
    )


def get_execution_engine(db: Session) -> BaseExecutionEngine:
    budget = build_default_loop_budget()
    backend = settings.execution_engine_backend

    if backend == "orchestrated":
        return OrchestratedExecutionEngine(budget=budget)

    raise ValueError(
        f"Unsupported execution_engine_backend '{backend}'. Supported backends: ['orchestrated']"
    )
