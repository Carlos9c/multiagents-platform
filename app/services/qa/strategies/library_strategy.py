"""QA strategy for reusable libraries, packages, and SDKs."""

from __future__ import annotations

from app.services.qa.strategies.base import QAStrategy


class LibraryStrategy(QAStrategy):
    """Reusable code libraries, packages, and SDKs."""

    @property
    def product_type(self) -> str:
        return "library"

    @property
    def requires_compiled_artifact(self) -> bool:
        return False

    @property
    def allowed_agents(self) -> list[str]:
        return [
            # Phase 5
            "functional_tester",
            "security_scanner",
            "contract_validator",
            # Phase 7-9
            "functional_qa_agent",
            "boundary_qa_agent",
            "adversarial_qa_agent",
            "regression_qa_agent",
            # Synthesis
            "synthesis_agent",
        ]
