from __future__ import annotations

from pydantic import ValidationError

from app.execution_engine.contracts import EXECUTION_DECISION_REJECTED
from app.models.execution_run import (
    EXECUTION_RUN_STATUS_FAILED,
    EXECUTION_RUN_STATUS_REJECTED,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema
from app.services.validation.router.prompt import (
    build_validation_router_system_prompt,
    build_validation_router_user_prompt,
)
from app.services.validation.router.schemas import (
    ValidationRoutingDecision,
    ValidationRoutingInput,
)


class ValidationRouterError(Exception):
    """Base exception for validation routing failures."""


def _derive_validation_mode(routing_input: ValidationRoutingInput) -> str:
    if routing_input.execution.execution_status == EXECUTION_RUN_STATUS_REJECTED:
        return "terminal_rejection"
    if routing_input.execution.execution_status == EXECUTION_RUN_STATUS_FAILED:
        return "terminal_failure"
    if routing_input.execution.decision == EXECUTION_DECISION_REJECTED:
        return "terminal_rejection"
    return "post_execution"


def _build_fallback_code_route(
    *,
    routing_input: ValidationRoutingInput,
    reason: str,
) -> ValidationRoutingDecision:
    return ValidationRoutingDecision.default_code_route(
        validation_mode=_derive_validation_mode(routing_input),
        routing_rationale=reason,
        validation_focus=[
            "acceptance_criteria_alignment",
            "scope_completion",
            "repository_changes",
            "constraint_compliance",
        ],
        open_questions=[],
    )


def resolve_validation_route(
    *,
    routing_input: ValidationRoutingInput,
) -> ValidationRoutingDecision:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        ValidationRoutingDecision.model_json_schema()
    )
    system_prompt = build_validation_router_system_prompt()
    user_prompt = build_validation_router_user_prompt(
        routing_input=routing_input,
    )

    try:
        raw = provider.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name="validation_routing_decision",
            json_schema=strict_schema,
        )
        decision = ValidationRoutingDecision.model_validate(raw)
    except ValidationError as exc:
        return _build_fallback_code_route(
            routing_input=routing_input,
            reason=(
                "Routing model output was structurally invalid, so the system "
                "fell back to the default code validator route. "
                f"Validation error: {str(exc)}"
            ),
        )
    except Exception as exc:
        return _build_fallback_code_route(
            routing_input=routing_input,
            reason=(
                "Routing model call failed, so the system fell back to the "
                f"default code validator route. Error: {str(exc)}"
            ),
        )

    expected_mode = _derive_validation_mode(routing_input)
    if decision.validation_mode != expected_mode:
        decision.validation_mode = expected_mode
        decision.routing_rationale = (
            f"{decision.routing_rationale} "
            f"Validation mode was normalized to '{expected_mode}' "
            "to match the execution outcome."
        ).strip()

    return decision
