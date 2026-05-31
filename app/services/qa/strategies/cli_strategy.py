"""QA strategy for CLI tools."""

from __future__ import annotations

from app.services.qa.strategies.base import QAStrategy


class CliToolStrategy(QAStrategy):
    """Command-line interface tools and scripts."""

    @property
    def product_type(self) -> str:
        return "cli_tool"

    @property
    def requires_compiled_artifact(self) -> bool:
        return False

    @property
    def allowed_agents(self) -> list[str]:
        return [
            # Phase 5
            "functional_tester",
            "security_scanner",
            # Phase 7-9
            "functional_qa_agent",
            "boundary_qa_agent",
            "adversarial_qa_agent",
            "performance_qa_agent",
            "regression_qa_agent",
            # Synthesis
            "synthesis_agent",
        ]
