from __future__ import annotations

from pydantic import ValidationError

from app.execution_engine.capabilities import render_executor_capabilities_for_prompt
from app.models.task import VALID_EXECUTOR_TYPES
from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.trace_writer import append_planning_trace

ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT = prompt_loader.get("atomic_task_generator")


def _validate_available_executors(available_executors: list[str]) -> list[str]:
    invalid_executors = [
        executor for executor in available_executors if executor not in VALID_EXECUTOR_TYPES
    ]
    if invalid_executors:
        raise ValueError(
            "Invalid available_executors values: "
            f"{invalid_executors}. Allowed values: {sorted(VALID_EXECUTOR_TYPES)}"
        )
    return list(available_executors)


def _build_executor_capabilities_text(available_executors: list[str]) -> str:
    executors = _validate_available_executors(available_executors)
    blocks: list[str] = []
    for executor in executors:
        blocks.append(render_executor_capabilities_for_prompt(executor))
    return "\n\n".join(blocks)


def build_atomic_user_prompt(
    *,
    project_name: str,
    project_description: str,
    parent_task_title: str,
    parent_task_description: str,
    parent_task_summary: str,
    parent_task_objective: str,
    parent_task_type: str,
    parent_task_planning_level: str,
    parent_task_proposed_solution: str,
    parent_task_implementation_steps: str,
    parent_task_acceptance_criteria: str,
    parent_task_tests_required: str,
    parent_task_technical_constraints: str,
    parent_task_out_of_scope: str,
    available_executors: list[str],
) -> str:
    executors = _validate_available_executors(available_executors)
    executors_text = "\n".join(f"- {executor}" for executor in executors)
    capability_text = _build_executor_capabilities_text(executors)

    prompt_loader.validate_builder_inputs(
        "atomic_task_generator",
        "main",
        {
            "project_name": project_name,
            "project_description": project_description,
            "parent_task_title": parent_task_title,
            "parent_task_description": parent_task_description,
            "parent_task_summary": parent_task_summary,
            "parent_task_objective": parent_task_objective,
            "parent_task_type": parent_task_type,
            "parent_task_planning_level": parent_task_planning_level,
            "parent_task_proposed_solution": parent_task_proposed_solution,
            "parent_task_implementation_steps": parent_task_implementation_steps,
            "parent_task_acceptance_criteria": parent_task_acceptance_criteria,
            "parent_task_tests_required": parent_task_tests_required,
            "parent_task_technical_constraints": parent_task_technical_constraints,
            "parent_task_out_of_scope": parent_task_out_of_scope,
            "available_executors": executors_text,
            "executor_capability_catalogs": capability_text,
        },
    )
    return f"""
Project name: {project_name}
Project description: {project_description}

Parent task to atomize:
- title: {parent_task_title}
- description: {parent_task_description}
- summary: {parent_task_summary}
- objective: {parent_task_objective}
- task_type: {parent_task_type}
- planning_level: {parent_task_planning_level}
- proposed_solution: {parent_task_proposed_solution}
- implementation_steps: {parent_task_implementation_steps}
- acceptance_criteria: {parent_task_acceptance_criteria}
- tests_required: {parent_task_tests_required}
- technical_constraints: {parent_task_technical_constraints}
- out_of_scope: {parent_task_out_of_scope}

Available executors:
{executors_text}

Executor capability catalogs:
{capability_text}

Mandatory instructions:
- Generate atomic tasks only.
- Each atomic task must be executable by exactly one available executor.
- Do NOT output a final executor assignment.
- Use the available executor list only to decide whether the task is truly executable by the current system.
- Never invent executors.
- Judge atomicity using BOTH:
  1) one primary deliverable and one validation boundary
  2) real executor capability compatibility
- Prefer the smallest number of tasks that remain truly executable.
- Avoid overlap.
- For the execution engine, rely on the listed subagents and tools instead of generic assumptions.

For the execution engine specifically:
- Prefer tasks that end in a concrete repository deliverable.
- Good deliverables include source files, test files, config files, or repository documentation files.
- Bad deliverables include open-ended investigation, manual verification, and external information gathering.
- Do not assign the execution engine a task whose core output depends on observing runtime behavior manually.
- Do not assign the execution engine a task whose core output is “analyze and document findings” unless the analysis is directly tied to a concrete repository artifact that can be produced from repo context.
- Implementation tasks must not write test files — test file creation is the exclusive
  deliverable of testing tasks. When the parent task mixes both, split them into separate
  atomic tasks: one for the implementation and one for the tests.

Split when:
- there are clearly separate deliverables that can be validated independently
- there are clearly separate validation boundaries
- executable work is mixed with manual/research work
- two feature slices or functional areas can be completed independently
- the scope covers an entire module, layer, subsystem, or "core" of the project
- there are multiple natural completion checkpoints within the task

Do NOT split when:
- the deliverable is one focused functional unit with a single validation boundary
- tightly-coupled artifacts have no independent validation boundary on their own
- a feature slice together with its directly-tied verification is modest in combined scope

Important:
- Rewrite the task around what the executor can actually finish.
- If a portion of the parent task is not executable by the available executors, do not make that non-executable portion the core of an atomic task.
- Prefer tasks that are individually completable and verifiable over tasks that are oversized and produce partial results.
- A task may be broader than a single file or class as long as it addresses one focused functional concern with one validation boundary.
""".strip()


def build_atomic_retry_prompt(
    *,
    validation_error: str,
    project_name: str,
    parent_task_title: str,
    available_executors: list[str],
) -> str:
    executors = _validate_available_executors(available_executors)
    executors_text = ", ".join(executors)
    capability_text = _build_executor_capabilities_text(executors)
    prompt_loader.validate_builder_inputs(
        "atomic_task_generator",
        "retry",
        {
            "project_name": project_name,
            "parent_task_title": parent_task_title,
            "available_executors": available_executors,
            "executor_capabilities": capability_text,
            "validation_error": validation_error,
        },
    )
    return f"""
Project name: {project_name}
Parent task title: {parent_task_title}

Your previous output was invalid.

Validation error:
{validation_error}

Available executors: {executors_text}

Executor capability catalogs:
{capability_text}

You must correct the output and return valid JSON matching the schema.

Important corrections:
- output only atomic tasks
- do not include executor_type in the output
- use the available executor list only as a capability constraint for atomicity
- each atomic task must have one primary deliverable and one validation boundary
- each atomic task must be compatible with the REAL capabilities of the assigned executor
- do not invent future executors or hypothetical capabilities
- for the execution engine, require a concrete repository/file deliverable
- use the listed execution-engine subagents and tools as the source of truth for what is realistically doable
- do not make manual investigation, external research, or manual validation the core of an execution engine task
- avoid overlap
- do not over-fragment
- split only when there are clearly separate deliverables, clearly separate validation boundaries, or executable and non-executable work are mixed
""".strip()


def call_atomic_task_generator_model(
    *,
    project_name: str,
    project_description: str,
    parent_task_title: str,
    parent_task_description: str,
    parent_task_summary: str,
    parent_task_objective: str,
    parent_task_type: str,
    parent_task_planning_level: str,
    parent_task_proposed_solution: str,
    parent_task_implementation_steps: str,
    parent_task_acceptance_criteria: str,
    parent_task_tests_required: str,
    parent_task_technical_constraints: str,
    parent_task_out_of_scope: str,
    available_executors: list[str],
    project_id: int | None = None,
    parent_task_id: int | None = None,
    call_type: str = "initial",
) -> AtomicTaskGenerationOutput:
    provider = get_llm_provider()
    first_user_prompt = build_atomic_user_prompt(
        project_name=project_name,
        project_description=project_description,
        parent_task_title=parent_task_title,
        parent_task_description=parent_task_description,
        parent_task_summary=parent_task_summary,
        parent_task_objective=parent_task_objective,
        parent_task_type=parent_task_type,
        parent_task_planning_level=parent_task_planning_level,
        parent_task_proposed_solution=parent_task_proposed_solution,
        parent_task_implementation_steps=parent_task_implementation_steps,
        parent_task_acceptance_criteria=parent_task_acceptance_criteria,
        parent_task_tests_required=parent_task_tests_required,
        parent_task_technical_constraints=parent_task_technical_constraints,
        parent_task_out_of_scope=parent_task_out_of_scope,
        available_executors=available_executors,
    )

    raw = provider.generate_structured(
        system_prompt=ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="atomic_task_generation_output",
        json_schema=AtomicTaskGenerationOutput.model_json_schema(),
    )

    try:
        result = AtomicTaskGenerationOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_atomic_retry_prompt(
            validation_error=str(exc),
            project_name=project_name,
            parent_task_title=parent_task_title,
            available_executors=available_executors,
        )

        raw_retry = provider.generate_structured(
            system_prompt=ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="atomic_task_generation_output",
            json_schema=AtomicTaskGenerationOutput.model_json_schema(),
        )

        result = AtomicTaskGenerationOutput.model_validate(raw_retry)

    if project_id is not None:
        append_planning_trace(
            project_id=project_id,
            entry={
                "agent": "atomic_task_generator",
                "call_type": call_type,
                "project_id": project_id,
                "inputs": {
                    "parent_task_id": parent_task_id,
                    "parent_task_title": parent_task_title,
                    "parent_task_acceptance_criteria": parent_task_acceptance_criteria,
                    "available_executors": available_executors,
                },
                "reasoning": result.generation_summary,
                "atomic_tasks_produced_count": len(result.atomic_tasks),
                "atomic_task_titles": [t.title for t in result.atomic_tasks],
                "output_snapshot": result.model_dump(),
            },
        )

    return result
