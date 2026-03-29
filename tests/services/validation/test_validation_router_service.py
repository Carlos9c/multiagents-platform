from app.services.validation.router.registry import (
    list_validation_router_catalog,
    render_validation_router_catalog,
)
from app.services.validation.router.schemas import (
    ValidationRoutingDecision,
    ValidationRoutingEvidenceSummary,
    ValidationRoutingExecutionSummary,
    ValidationRoutingInput,
    ValidationRoutingTaskContext,
)
from app.services.validation.router.service import resolve_validation_route


class _FakeProvider:
    def __init__(self, payload):
        self._payload = payload

    def generate_structured(self, **kwargs):
        if callable(self._payload):
            return self._payload(**kwargs)
        return self._payload


def test_resolve_validation_route_returns_llm_decision(monkeypatch):
    routing_input = ValidationRoutingInput(
        task=ValidationRoutingTaskContext(
            task_id=1,
            project_id=10,
            title="Implement API endpoint and tests",
            description="Add the endpoint and update the related tests.",
            summary="Repository implementation work.",
            objective="Deliver the endpoint with passing tests.",
            acceptance_criteria="Endpoint exists and tests cover the new behavior.",
            technical_constraints="Keep the existing structure.",
            out_of_scope="No production deployment.",
            task_type="implementation",
            planning_level="atomic",
            executor_type="execution_engine",
        ),
        execution=ValidationRoutingExecutionSummary(
            execution_run_id=100,
            execution_status="succeeded",
            decision="completed",
            summary="Implementation completed.",
            details="Changed endpoint and test files.",
            completed_scope="Endpoint and tests updated.",
            remaining_scope=None,
            blockers_found=[],
            validation_notes=["Execution finished normally."],
            output_snapshot="done",
            execution_agent_sequence=["planner", "editor", "tester"],
        ),
        evidence=ValidationRoutingEvidenceSummary(
            changed_file_paths=["app/api/routes.py", "tests/test_routes.py"],
            command_count=2,
            artifact_refs=["artifact_id=11"],
            evidence_notes=["Tests were executed."],
            relevant_files=["app/api/routes.py", "tests/test_routes.py"],
            allowed_paths=["app/", "tests/"],
            key_decisions=["Maintain current API structure."],
            related_task_ids=[2, 3],
        ),
    )

    expected = ValidationRoutingDecision.default_code_route(
        validation_mode="post_execution",
        routing_rationale=(
            "The task is repository implementation work and should be "
            "validated by the code validator."
        ),
    )

    monkeypatch.setattr(
        "app.services.validation.router.service.get_llm_provider",
        lambda: _FakeProvider(expected.model_dump(mode="json")),
    )

    decision = resolve_validation_route(
        routing_input=routing_input,
    )

    assert decision.validator_key == "code_task_validator"
    assert decision.discipline == "code"
    assert decision.validation_mode == "post_execution"
    assert decision.requires_workspace is True
    assert decision.requires_file_reading is True
    assert decision.requires_changed_files is True
    assert decision.requires_command_results is True
    assert decision.requires_artifacts is True


def test_resolve_validation_route_normalizes_terminal_failure_mode(monkeypatch):
    routing_input = ValidationRoutingInput(
        task=ValidationRoutingTaskContext(
            task_id=1,
            project_id=10,
            title="Fix failing implementation task",
        ),
        execution=ValidationRoutingExecutionSummary(
            execution_run_id=100,
            execution_status="failed",
            decision="failed",
            summary="Execution failed.",
        ),
        evidence=ValidationRoutingEvidenceSummary(),
    )

    raw = ValidationRoutingDecision.default_code_route(
        validation_mode="post_execution",
        routing_rationale="Route to code validator.",
    ).model_dump(mode="json")

    monkeypatch.setattr(
        "app.services.validation.router.service.get_llm_provider",
        lambda: _FakeProvider(raw),
    )

    decision = resolve_validation_route(
        routing_input=routing_input,
    )

    assert decision.validation_mode == "terminal_failure"
    assert "normalized to 'terminal_failure'" in decision.routing_rationale


def test_resolve_validation_route_falls_back_to_default_code_route_on_invalid_llm_output(
    monkeypatch,
):
    routing_input = ValidationRoutingInput(
        task=ValidationRoutingTaskContext(
            task_id=1,
            project_id=10,
            title="Implement repository change",
        ),
        execution=ValidationRoutingExecutionSummary(
            execution_run_id=100,
            execution_status="succeeded",
            decision="completed",
            summary="Execution completed.",
        ),
        evidence=ValidationRoutingEvidenceSummary(),
    )

    monkeypatch.setattr(
        "app.services.validation.router.service.get_llm_provider",
        lambda: _FakeProvider({"validator_key": "x"}),
    )

    decision = resolve_validation_route(
        routing_input=routing_input,
    )

    assert decision.validator_key == "code_task_validator"
    assert decision.discipline == "code"
    assert decision.validation_mode == "post_execution"
    assert "fell back to the default code validator route" in decision.routing_rationale


def test_validation_router_catalog_contains_code_validator():
    catalog = list_validation_router_catalog()

    assert len(catalog) >= 1
    assert any(entry.validator_key == "code_task_validator" for entry in catalog)


def test_render_validation_router_catalog_includes_validator_metadata():
    rendered = render_validation_router_catalog()

    assert "validator_key: code_task_validator" in rendered
    assert "discipline: code" in rendered
    assert "typical_deliverables:" in rendered
    assert "typical_evidence:" in rendered
