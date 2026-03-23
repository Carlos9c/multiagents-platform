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
- Each atomic task must be narrow, deterministic, directly executable, and assigned to exactly one executor.

Primary rule:
- Judge atomicity by one primary deliverable and one clear validation boundary.
- Do NOT split tasks just because the title contains "and".
- Do NOT split coherent requirements/specification/design/analysis/document tasks just because they mention several closely related aspects of the same deliverable.
- Do NOT over-fragment.

Granularity rules:
- Prefer coherent deliverable blocks.
- Avoid overlap between atomic tasks.
- Avoid duplicate scopes phrased differently.
- A single atomic task is valid if it has one clear deliverable and one validation boundary.
- Keep the number of atomic tasks compact.

When to split:
- Split only when there are clearly separate deliverables or clearly separate phases.
- Typical split cases:
  - analysis/recommendation + implementation
  - documentation/specification + implementation
  - implementation + deployment/infrastructure
  - several independent feature slices implemented together
  - substantial drafting + substantial final compilation of many sections

When NOT to split:
- one comparison/recommendation
- one requirements/specification section
- one contract block
- one design decision plus its minimal structural definition
- one coherent functional scope definition
- one coherent data model + request/response specification for the same small feature
- one coherent document section even if it mentions GET and POST for the same small API
- one implementation slice that still has one clear validation boundary

Executor rules:
- Assign exactly one executor_type to each atomic task.
- Use only executors from the provided list.
- Never invent new executors.

Hard requirements:
- proposed_solution must explain the immediate approach.
- implementation_steps must be a list of short concrete steps.
- tests_required must define how the atomic task should be verified.
- acceptance_criteria must be a single string.
- Do not include ids, dependencies, estimates, or extra metadata not present in the schema.

Critical instruction:
- The downstream system has executor, validator, recovery, and re-atomization layers.
- Do not be artificially conservative.
- Prefer allowing a coherent atomic task to proceed rather than over-splitting it.
- Only split when the semantic boundary is clearly real.

Self-check before finalizing:
- each task has one primary deliverable
- each task has one validation boundary
- there is minimal overlap between tasks
- tasks are not over-fragmented
- coherent non-implementation tasks may include several closely related sub-aspects inside one atomic deliverable
- do not split only because of grammatical conjunctions
""".strip()


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
- Use one primary deliverable and one validation boundary as the atomicity criterion.
- Do not split only because the task title has multiple verbs or conjunctions.
- Do not split coherent non-implementation tasks because they include several closely related aspects of the same deliverable.
- Avoid overlap.
- Keep the number of tasks compact.
- Prefer letting a coherent atomic task proceed instead of over-splitting it.

Split only when:
- there are clearly separate deliverables
- there are clearly separate phases
- there is substantial implementation mixed with documentation/analysis/deployment
- there are clearly independent features being implemented together

If one coherent atomic task is enough, output one.
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
- judge atomicity by one primary deliverable and one validation boundary
- do not split only because of conjunctions
- do not over-fragment coherent work
- avoid overlap
- split only when there are clearly separate deliverables or clearly separate phases
- prefer coherent atomic tasks over artificially tiny fragments
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