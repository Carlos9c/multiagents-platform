"""QA strategy for web applications."""

from __future__ import annotations

from app.services.qa.strategies.base import QAStrategy


class WebAppStrategy(QAStrategy):
    """Browser-based web applications (React, Vue, Django, Rails, etc.)."""

    @property
    def product_type(self) -> str:
        return "web_app"

    @property
    def requires_compiled_artifact(self) -> bool:
        return False

    @property
    def allowed_agents(self) -> list[str]:
        return [
            # Phase 5
            "functional_tester",
            "security_scanner",
            "performance_profiler",
            # Phase 7-9
            "functional_qa_agent",
            "boundary_qa_agent",
            "adversarial_qa_agent",
            "security_qa_agent",
            "performance_qa_agent",
            "regression_qa_agent",
            # Synthesis
            "synthesis_agent",
        ]
