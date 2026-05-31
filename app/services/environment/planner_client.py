from __future__ import annotations

from pydantic import ValidationError

from app.schemas.environment import RuntimeEnvironmentPlanOutput, RuntimeSpecRepairOutput
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.trace_writer import append_planning_trace

ENVIRONMENT_PLANNER_SYSTEM_PROMPT = prompt_loader.get("environment_planner")


def build_environment_planner_user_prompt(
    project_name: str,
    project_description: str,
    catalog_hint: str | None = None,
    existing_environment_context: str | None = None,
) -> str:
    prompt_loader.validate_builder_inputs(
        "environment_planner",
        "main",
        {
            "project_name": project_name,
            "project_description": project_description,
            "catalog_hint": catalog_hint,
            "existing_environment_context": existing_environment_context,
        },
    )

    hint_section = ""
    if catalog_hint:
        hint_section = f"""
Pre-selected base image: {catalog_hint}
The runtime_type and image fields must match this pre-selected environment.
Choose dependencies that are compatible with and installable in this image.
"""

    context_section = ""
    if existing_environment_context:
        context_section = f"""
Existing environment (detected manifest files):
{existing_environment_context}

Build upon and extend this existing environment.
Preserve all pinned versions unless there is a direct conflict.
The runtime_type must match the ecosystem above.
"""

    return f"""
Project name: {project_name}
Project description: {project_description}
{hint_section}{context_section}
Planning instructions:
- Determine the runtime (python_venv, node_npm, rust_cargo, java_maven, java_gradle, android_gradle, react_native, dotnet, or go) that can implement this project.
- Choose an appropriate Docker image as the base.
- List every package required to implement and test the project.
- Pin every package to an exact version (x.y.z). No ranges.
- For build-system-managed runtimes (android_gradle, java_gradle, java_maven, rust_cargo, dotnet, go): return an empty dependencies list.
- When a library is not specified, choose the most modern production-ready option (2024-2025).
- Include the test runner appropriate for the runtime.
- Do not include packages already provided by the runtime standard library.
""".strip()


def build_environment_planner_retry_prompt(
    project_name: str,
    project_description: str,
    validation_error: str,
    catalog_hint: str | None = None,
    existing_environment_context: str | None = None,
) -> str:
    from app.services.prompt_loader import prompt_loader

    prompt_loader.validate_builder_inputs(
        "environment_planner",
        "retry",
        {
            "project_name": project_name,
            "project_description": project_description,
            "catalog_hint": catalog_hint,
            "existing_environment_context": existing_environment_context,
            "validation_error": validation_error,
        },
    )
    base = build_environment_planner_user_prompt(
        project_name,
        project_description,
        catalog_hint=catalog_hint,
        existing_environment_context=existing_environment_context,
    )
    return f"""Your previous output was invalid.

Validation error:
{validation_error}

You must correct your output and return valid JSON matching the schema.

Key rules:
- runtime_type must be exactly one of: python_venv, node_npm, rust_cargo, java_maven, java_gradle, android_gradle, react_native, dotnet, go.
- image must be a valid Docker image name (e.g. "python:3.12-slim").
- Every dependency version must be an exact x.y.z string.
- planning_rationale must not be empty.

{base}""".strip()


def build_environment_repair_user_prompt(
    spec_dict: dict,
    smoke_test_command: str,
    error_output: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "environment_planner",
        "repair",
        {
            "current_spec": spec_dict,
            "smoke_test_command": smoke_test_command,
            "error_output": error_output,
        },
    )
    return f"""The current runtime environment failed its smoke test.

Current spec:
{spec_dict}

Smoke test command that was run:
{smoke_test_command}

Error output:
{error_output}

Repair instructions:
- Identify the root cause of the failure from the error output.
- Common causes: wrong package name, incompatible version combination, missing package, wrong image.
- Return a corrected dependency list and image.
- Keep changes minimal — only fix what is broken.
- All versions must remain exact x.y.z format.
- If the image needs to change, provide the corrected image.
""".strip()


ENVIRONMENT_REPAIR_SYSTEM_PROMPT = prompt_loader.get("environment_planner", "repair")


def call_environment_planner(
    project_name: str,
    project_description: str,
    catalog_hint: str | None = None,
    existing_environment_context: str | None = None,
    project_id: int | None = None,
) -> RuntimeEnvironmentPlanOutput:
    provider = get_llm_provider()
    user_prompt = build_environment_planner_user_prompt(
        project_name=project_name,
        project_description=project_description,
        catalog_hint=catalog_hint,
        existing_environment_context=existing_environment_context,
    )

    raw = provider.generate_structured(
        system_prompt=ENVIRONMENT_PLANNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="runtime_environment_plan",
        json_schema=RuntimeEnvironmentPlanOutput.model_json_schema(),
    )

    try:
        result = RuntimeEnvironmentPlanOutput.model_validate(raw)
    except ValidationError as exc:
        retry_prompt = build_environment_planner_retry_prompt(
            project_name=project_name,
            project_description=project_description,
            validation_error=str(exc),
            catalog_hint=catalog_hint,
            existing_environment_context=existing_environment_context,
        )
        raw_retry = provider.generate_structured(
            system_prompt=ENVIRONMENT_PLANNER_SYSTEM_PROMPT,
            user_prompt=retry_prompt,
            schema_name="runtime_environment_plan",
            json_schema=RuntimeEnvironmentPlanOutput.model_json_schema(),
        )
        result = RuntimeEnvironmentPlanOutput.model_validate(raw_retry)

    if project_id is not None:
        append_planning_trace(
            project_id=project_id,
            entry={
                "agent": "environment_planner",
                "call_type": "initial",
                "project_id": project_id,
                "inputs": {
                    "project_name": project_name,
                    "has_existing_environment_context": existing_environment_context is not None,
                    "catalog_hint": catalog_hint,
                },
                "reasoning": result.planning_rationale,
                "output_snapshot": {
                    "runtime_type": result.runtime_type,
                    "image": result.image,
                    "dependencies_count": len(result.dependencies),
                    "env_vars_count": len(result.environment_variables),
                },
            },
        )

    return result


def call_environment_repair(
    spec_dict: dict,
    smoke_test_command: str,
    error_output: str,
    project_id: int | None = None,
) -> RuntimeSpecRepairOutput:
    provider = get_llm_provider()
    user_prompt = build_environment_repair_user_prompt(
        spec_dict=spec_dict,
        smoke_test_command=smoke_test_command,
        error_output=error_output,
    )

    raw = provider.generate_structured(
        system_prompt=ENVIRONMENT_REPAIR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="runtime_spec_repair",
        json_schema=RuntimeSpecRepairOutput.model_json_schema(),
    )

    try:
        result = RuntimeSpecRepairOutput.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Environment repair LLM returned invalid output: {exc}") from exc

    if project_id is not None:
        append_planning_trace(
            project_id=project_id,
            entry={
                "agent": "environment_planner",
                "call_type": "repair",
                "project_id": project_id,
                "inputs": {
                    "smoke_test_command": smoke_test_command,
                    "error_output": error_output,
                },
                "reasoning": result.repair_rationale,
                "output_snapshot": {
                    "corrected_image": result.corrected_image,
                    "corrected_dependencies_count": len(result.corrected_dependencies),
                },
            },
        )

    return result
