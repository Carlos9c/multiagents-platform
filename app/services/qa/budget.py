"""QA Engine loop budget.

Controls how many probing steps the QA orchestrator may take before
forcing synthesis. Analogous to execution_engine/budget.py.
"""

from pydantic import BaseModel


class QABudget(BaseModel):
    # ── Legacy fields (kept for backward compat) ──────────────────────────────
    max_steps: int = 12
    max_agent_calls: int = 10
    max_probes_per_agent: int = 5

    # ── LLM-driven orchestrator budget ────────────────────────────────────────
    # Maximum number of LLM decision rounds in the probing phase.
    max_probe_rounds: int = 8

    # Wall-clock timeout for the entire probing phase (seconds).
    timeout_seconds: float = 300.0

    # Auto-finish probing when this many findings have been accumulated.
    # Prevents running all agents when critical issues already found.
    max_findings: int = 20
