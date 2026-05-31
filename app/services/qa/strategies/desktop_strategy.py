"""QA strategy for native desktop applications."""

from __future__ import annotations

from app.services.qa.strategies.base import QAStrategy


class DesktopAppStrategy(QAStrategy):
    """Native desktop applications (Electron, Qt, WPF, Tauri, etc.)."""

    @property
    def product_type(self) -> str:
        return "desktop_app"

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
            "regression_qa_agent",
            # Synthesis
            "synthesis_agent",
        ]
