"""QA agent registry.

Analogous to execution_engine/subagent_registry.py.
Concrete agent instances are registered here and resolved by name
during the QA orchestrator loop.
"""

from __future__ import annotations

from app.services.qa.agents.base import BaseQAAgent


class QAAgentRegistryError(Exception):
    """Raised when the registry cannot resolve a QA agent."""


class QAAgentRegistry:
    def __init__(self, agents: list[BaseQAAgent]) -> None:
        self._agents: dict[str, BaseQAAgent] = {a.name: a for a in agents}

    def get(self, name: str) -> BaseQAAgent | None:
        """Return the agent with the given name, or None if not registered."""
        return self._agents.get(name)

    def require(self, name: str) -> BaseQAAgent:
        """Return the agent with the given name, raising if not registered."""
        agent = self._agents.get(name)
        if agent is None:
            raise QAAgentRegistryError(f"QA agent {name!r} is not registered")
        return agent

    def all_names(self) -> list[str]:
        return sorted(self._agents.keys())


# Module-level registry populated by Phase 5 agent registrations.
# Starts empty; agents are added when their modules are imported.
_default_registry: QAAgentRegistry = QAAgentRegistry([])


def get_agent_registry() -> QAAgentRegistry:
    return _default_registry


def register_agents(agents: list[BaseQAAgent]) -> None:
    """Replace the default registry with a new one containing the given agents.

    Called once at startup (or in tests) to wire up concrete agent instances.
    """
    global _default_registry
    _default_registry = QAAgentRegistry(agents)


def build_default_registry() -> QAAgentRegistry:
    """Instantiate and return a registry with all built-in QA agents."""
    from app.services.qa.agents.apk_installer import ApkInstallerAgent
    from app.services.qa.agents.contract_validator import ContractValidatorAgent
    from app.services.qa.agents.functional_tester import FunctionalTesterAgent
    from app.services.qa.agents.performance_profiler import PerformanceProfilerAgent
    from app.services.qa.agents.security_scanner import SecurityScannerAgent
    from app.services.qa.agents.synthesis_agent import SynthesisAgentImpl
    from app.services.qa.qa_agents.adversarial_qa_agent import AdversarialQAAgent
    from app.services.qa.qa_agents.boundary_qa_agent import BoundaryQAAgent
    from app.services.qa.qa_agents.functional_qa_agent import FunctionalQAAgent
    from app.services.qa.qa_agents.performance_qa_agent import PerformanceQAAgent
    from app.services.qa.qa_agents.regression_qa_agent import RegressionQAAgent
    from app.services.qa.qa_agents.security_qa_agent import SecurityQAAgent

    return QAAgentRegistry(
        [
            # Phase 5 agents
            FunctionalTesterAgent(),
            SecurityScannerAgent(),
            ContractValidatorAgent(),
            PerformanceProfilerAgent(),
            ApkInstallerAgent(),
            SynthesisAgentImpl(),
            # Phase 7 agents
            FunctionalQAAgent(),
            BoundaryQAAgent(),
            # Phase 8 agents
            AdversarialQAAgent(),
            SecurityQAAgent(),
            # Phase 9 agents
            PerformanceQAAgent(),
            RegressionQAAgent(),
        ]
    )
