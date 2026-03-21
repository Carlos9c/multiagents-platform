from pydantic import ValidationError

from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT = """
You are a senior atomic task generation agent.

Your job is to convert one refined project task into a set of atomic tasks.
Return ONLY JSON matching the provided schema.

Core mission:
- Transform one refined task into atomic tasks.
- Atomic tasks must be the smallest useful execution units for the current system.
- Each atomic task must be narrow, deterministic, and directly executable by exactly one executor.

Executor selection rules:
- You will receive a list of currently available executors.
- You MUST assign executor_type for each atomic task using only one value from that list.
- Never invent a new executor.
- Never leave executor_type empty.
- If only one executor is available, assign that executor to every atomic task.
- If a refined task contains work that would normally belong to different executor types, decompose it into atomic tasks and assign each one to the most appropriate available executor.
- If the ideal executor is not available, reformulate the atomic task conservatively so it can still be handled by one of the available executors.

Granularity rules:
- Atomic tasks must be smaller than refined tasks.
- Do not create broad atomic tasks like "implement authentication module".
- Prefer concrete outputs such as creating a file, implementing a method, adding a route, writing a focused document section, or adding a specific validation.
- Keep atomic tasks testable and verifiable.

Platform perspective:
- Internal platform entities such as Project, Task, Artifact, and ExecutionRun are orchestration concepts.
- Do not assume the user's project domain must reuse those internal entities unless explicitly requested.

Hard requirements:
- The input task is refined and not yet directly executable.
- Your output must decompose it into atomic tasks.
- Each atomic task must be concrete, bounded, deterministic, and assigned to exactly one available executor.
- proposed_solution must explain the immediate implementation approach.
- implementation_steps must be a list of short concrete steps.
- tests_required must define how the atomic task should be verified.
- acceptance_criteria must be a single string.
- Do not include ids, dependencies, estimates, or extra metadata not present in the schema.

Self-check:
Before finalizing, silently verify:
- each atomic task is smaller and more executable than the parent refined task
- each atomic task has exactly one executor from the available list
- the set of atomic tasks is complete enough to move the refined task forward
- there are no filler tasks
"""


def build_atomic_user_prompt(
    *,
    project_name: str,
    project_description: str,
    refined_task_title: str,
    refined_task_description: str,
    refined_task_summary: str,
    refined_task_objective: str,
    refined_task_type: str,
    refined_task_proposed_solution: str,
    refined_task_implementation_steps: str,
    refined_task_acceptance_criteria: str,
    refined_task_tests_required: str,
    refined_task_technical_constraints: str,
    refined_task_out_of_scope: str,
    available_executors: list[str],
) -> str:
    executors_text = "\n".join(f"- {executor}" for executor in available_executors)

    return f"""
Project name: {project_name}
Project description: {project_description}

Parent refined task:
- title: {refined_task_title}
- description: {refined_task_description}
- summary: {refined_task_summary}
- objective: {refined_task_objective}
- task_type: {refined_task_type}
- proposed_solution: {refined_task_proposed_solution}
- implementation_steps: {refined_task_implementation_steps}
- acceptance_criteria: {refined_task_acceptance_criteria}
- tests_required: {refined_task_tests_required}
- technical_constraints: {refined_task_technical_constraints}
- out_of_scope: {refined_task_out_of_scope}

Available executors:
{executors_text}

Important:
- Generate atomic tasks only.
- Each atomic task must be directly executable by exactly one available executor.
- Choose executor_type only from the available executor list.
- Do not invent new executors.
- Today the platform may have only one executor available; if so, assign it consistently.
""".strip()


def build_atomic_retry_prompt(
    *,
    project_name: str,
    refined_task_title: str,
    validation_error: str,
    available_executors: list[str],
) -> str:
    executors_text = ", ".join(available_executors)

    return f"""
Project name: {project_name}
Parent refined task title: {refined_task_title}

Your previous output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Important corrections:
- output only atomic tasks
- each atomic task must have exactly one executor_type
- executor_type must be one of: {executors_text}
- do not invent executors
- do not output refined-level or vague tasks
- keep tasks small, concrete, and executable
""".strip()


def call_atomic_task_generator_model(
    *,
    project_name: str,
    project_description: str,
    refined_task_title: str,
    refined_task_description: str,
    refined_task_summary: str,
    refined_task_objective: str,
    refined_task_type: str,
    refined_task_proposed_solution: str,
    refined_task_implementation_steps: str,
    refined_task_acceptance_criteria: str,
    refined_task_tests_required: str,
    refined_task_technical_constraints: str,
    refined_task_out_of_scope: str,
    available_executors: list[str],
) -> AtomicTaskGenerationOutput:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        AtomicTaskGenerationOutput.model_json_schema()
    )

    first_user_prompt = build_atomic_user_prompt(
        project_name=project_name,
        project_description=project_description,
        refined_task_title=refined_task_title,
        refined_task_description=refined_task_description,
        refined_task_summary=refined_task_summary,
        refined_task_objective=refined_task_objective,
        refined_task_type=refined_task_type,
        refined_task_proposed_solution=refined_task_proposed_solution,
        refined_task_implementation_steps=refined_task_implementation_steps,
        refined_task_acceptance_criteria=refined_task_acceptance_criteria,
        refined_task_tests_required=refined_task_tests_required,
        refined_task_technical_constraints=refined_task_technical_constraints,
        refined_task_out_of_scope=refined_task_out_of_scope,
        available_executors=available_executors,
    )

    raw = provider.generate_structured(
        system_prompt=ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="atomic_task_generation_output",
        json_schema=strict_schema,
    )

    try:
        return AtomicTaskGenerationOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_atomic_retry_prompt(
            project_name=project_name,
            refined_task_title=refined_task_title,
            validation_error=str(exc),
            available_executors=available_executors,
        )
        raw_retry = provider.generate_structured(
            system_prompt=ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="atomic_task_generation_output",
            json_schema=strict_schema,
        )
        return AtomicTaskGenerationOutput.model_validate(raw_retry)