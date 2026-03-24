from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.execution_engine.base import BaseExecutionEngine
from app.execution_engine.budget import LoopBudget
from app.execution_engine.engines.legacy_local_engine import LegacyLocalExecutionEngine
from app.execution_engine.engines.orchestrated_engine import OrchestratedExecutionEngine


def build_default_loop_budget() -> LoopBudget:
    return LoopBudget(
        max_steps=settings.execution_engine_max_steps,
        max_agent_calls=settings.execution_engine_max_agent_calls,
        max_tool_calls=settings.execution_engine_max_tool_calls,
        max_command_runs=settings.execution_engine_max_command_runs,
        max_repair_attempts=settings.execution_engine_max_repair_attempts,
    )


def get_execution_engine(db: Session) -> BaseExecutionEngine:
    backend = settings.execution_engine_backend
    budget = build_default_loop_budget()

    if backend == "legacy_local":
        return LegacyLocalExecutionEngine(db=db, budget=budget)

    if backend == "orchestrated":
        return OrchestratedExecutionEngine(budget=budget)

    raise ValueError(
        f"Unsupported execution_engine_backend '{backend}'. "
        "Supported backends: ['legacy_local', 'orchestrated']"
    )