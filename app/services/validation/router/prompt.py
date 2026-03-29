from __future__ import annotations

import json

from app.services.validation.router.registry import render_validation_router_catalog
from app.services.validation.router.schemas import ValidationRoutingInput


def build_validation_router_system_prompt() -> str:
    validator_catalog = render_validation_router_catalog()

    return f"""
You are a validation routing agent.

Your job is to decide which validator must validate a task execution result.

You MUST reason from:
- the task itself
- the execution outcome
- the available evidence summary

You do NOT validate the task itself.
You ONLY decide:
- which validator should handle validation
- which validation mode applies
- which evidence the validator must require
- what validation focus areas matter most

General routing rules:
- Do not invent validators.
- Use only validators that exist in the system.
- Choose the validator based on the real nature of the task and the evidence.
- Prefer the most specific validator that can evaluate the task correctly.
- Route according to the dominant validation discipline of the task deliverable.
- If execution ended in a terminal rejection, route using validation_mode=terminal_rejection.
- If execution ended in a terminal failure, route using validation_mode=terminal_failure.
- Otherwise use validation_mode=post_execution.
- If evidence appears insufficient for reliable validation, set require_manual_review_if_evidence_missing=true.
- routing_rationale must be specific, grounded, and must reference the task and evidence shape.
- validation_focus must describe what the chosen validator must check, not generic filler.
- open_questions should contain only materially relevant unresolved validation concerns.

Available validators:
{validator_catalog}

Return ONLY JSON matching the provided schema.
""".strip()


def build_validation_router_user_prompt(
    *,
    routing_input: ValidationRoutingInput,
) -> str:
    payload = routing_input.model_dump(mode="json")
    pretty_payload = json.dumps(payload, ensure_ascii=False, indent=2)

    return f"""
Decide which validator should validate this task execution result.

Routing input:
{pretty_payload}

Instructions:
- Read the task carefully.
- Read the execution outcome carefully.
- Read the evidence summary carefully.
- Choose the validator that is best suited to validate the task.
- Set validation_mode according to the execution outcome.
- Enable the required evidence flags conservatively but correctly.
- validation_focus should list the key validation concerns this validator must assess.
- open_questions should contain only unresolved issues that could materially affect validation quality.
- Do not validate the task here.
- Do not invent any validator not listed in the system prompt.
""".strip()
