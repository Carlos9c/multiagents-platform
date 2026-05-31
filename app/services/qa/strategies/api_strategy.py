"""QA strategies for REST and GraphQL API products."""

from __future__ import annotations

from app.services.qa.strategies.base import QAStrategy


class RestApiStrategy(QAStrategy):
    """REST API backend services."""

    @property
    def product_type(self) -> str:
        return "rest_api"

    @property
    def requires_compiled_artifact(self) -> bool:
        return False

    @property
    def allowed_agents(self) -> list[str]:
        return [
            # Phase 5 — execution-level probes
            "functional_tester",
            "security_scanner",
            "contract_validator",
            "performance_profiler",
            # Phase 7-9 — structured analysis probes
            "functional_qa_agent",
            "boundary_qa_agent",
            "adversarial_qa_agent",
            "security_qa_agent",
            "performance_qa_agent",
            "regression_qa_agent",
            # Synthesis (always last)
            "synthesis_agent",
        ]


class GraphqlApiStrategy(QAStrategy):
    """GraphQL API services."""

    @property
    def product_type(self) -> str:
        return "graphql_api"

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
            "security_qa_agent",
            "regression_qa_agent",
            # Synthesis
            "synthesis_agent",
        ]
