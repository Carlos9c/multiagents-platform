"""QAStrategy ABC.

Each product type gets a concrete strategy that defines:
- Which QA agents are allowed to run
- The probe ordering hint (agents listed first are called first)
- Whether a compiled artifact is required before probing starts
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class QAStrategy(ABC):
    """Base class for all product-type QA strategies."""

    # Ordered list of agent names this strategy may invoke.
    # The QA orchestrator respects this order as a hint but may skip
    # agents when their preconditions are not met.
    @property
    @abstractmethod
    def allowed_agents(self) -> list[str]:
        """Agent names that this strategy permits, in preferred call order."""
        ...

    @property
    @abstractmethod
    def requires_compiled_artifact(self) -> bool:
        """True if QABootstrapper must produce a binary/APK before probing."""
        ...

    @property
    @abstractmethod
    def product_type(self) -> str:
        """The product_type string this strategy covers."""
        ...

    def is_agent_allowed(self, agent_name: str) -> bool:
        return agent_name in self.allowed_agents
